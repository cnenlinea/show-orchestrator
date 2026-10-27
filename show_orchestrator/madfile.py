"""Read and write MadMapper .mad project files.

A .mad file is a Qt QDataStream: a 0x0BADBABE magic, a stream version, then a
QVariantMap. The stream version (12, Qt 4.6) means type ids use Qt4 numbering,
e.g. Float is 135 rather than 38, and floats are written as doubles.

Every value is decoded to a `(type_id, value, is_null)` tuple and maps to lists
of `(key, variant)` pairs, so a file loaded and saved unchanged comes back
byte-for-byte identical. Types that are only ever copied (colors, images,
geometry...) are kept as raw bytes.
"""
import struct
from pathlib import Path
from typing import Any

MAGIC = 0x0BADBABE

T_INVALID = 0
T_BOOL, T_INT, T_UINT, T_LONGLONG, T_ULONGLONG, T_DOUBLE = 1, 2, 3, 4, 5, 6
T_MAP, T_LIST, T_STRING, T_STRINGLIST, T_BYTEARRAY = 8, 9, 10, 11, 12
T_RECT, T_RECTF, T_SIZE, T_SIZEF, T_POINT, T_POINTF = 19, 20, 21, 22, 25, 26
T_UUID, T_COLOR, T_IMAGE, T_TRANSFORM = 30, 67, 70, 81
T_USER, T_FLOAT, T_JSONDOC = 127, 135, 145

Variant = tuple[int, Any, int]
VariantMap = list[tuple[str, Variant]]

_FIXED_SIZE = {T_UUID: 16, T_COLOR: 11, T_TRANSFORM: 72}
_STRUCTS = {
    T_INT: ">i", T_UINT: ">I", T_LONGLONG: ">q", T_ULONGLONG: ">Q",
    T_DOUBLE: ">d", T_FLOAT: ">d",
    T_SIZE: ">ii", T_POINT: ">ii", T_SIZEF: ">dd", T_POINTF: ">dd",
    T_RECT: ">iiii", T_RECTF: ">dddd",
}
_TUPLE_TYPES = {T_SIZE, T_POINT, T_SIZEF, T_POINTF, T_RECT, T_RECTF}


class MadFileError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise MadFileError(f"unexpected end of file at offset {self.pos}")
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def qstring(self) -> str | None:
        n = self.u32()
        return None if n == 0xFFFFFFFF else self.take(n).decode("utf-16-be")

    def qbytearray(self) -> bytes | None:
        n = self.u32()
        return None if n == 0xFFFFFFFF else self.take(n)

    def variant_map(self) -> VariantMap:
        return [(self.qstring(), self.variant()) for _ in range(self.u32())]

    def variant(self) -> Variant:
        start = self.pos
        type_id = self.u32()
        is_null = self.u8()
        return (type_id, self.value(type_id, start), is_null)

    def value(self, type_id: int, start: int) -> Any:
        if type_id == T_INVALID:
            return self.qstring()  # Qt4 streams follow an invalid variant with a QString
        if type_id == T_BOOL:
            return bool(self.u8())
        if type_id in _STRUCTS:
            fmt = _STRUCTS[type_id]
            values = struct.unpack(fmt, self.take(struct.calcsize(fmt)))
            return values if type_id in _TUPLE_TYPES else values[0]
        if type_id == T_STRING:
            return self.qstring()
        if type_id in (T_BYTEARRAY, T_JSONDOC):
            return self.qbytearray()
        if type_id == T_STRINGLIST:
            return [self.qstring() for _ in range(self.u32())]
        if type_id == T_LIST:
            return [self.variant() for _ in range(self.u32())]
        if type_id == T_MAP:
            return self.variant_map()
        if type_id in _FIXED_SIZE:
            return self.take(_FIXED_SIZE[type_id])
        if type_id == T_IMAGE:
            return self.image()
        if type_id == T_USER:
            return self.user_type(start)
        raise MadFileError(f"unsupported value type {type_id} at offset {start}")

    def image(self) -> bytes:
        # A QImage is a bare PNG with no length prefix, or a 0 int when null.
        if self.data[self.pos:self.pos + 8] != b"\x89PNG\r\n\x1a\n":
            return self.take(4)
        start = self.pos
        self.pos += 8
        while True:
            length = self.u32()
            chunk_type = self.take(4)
            self.pos += length + 4  # chunk data + CRC
            if chunk_type == b"IEND":
                return self.data[start:self.pos]

    def user_type(self, start: int) -> tuple[bytes, bytes]:
        """MadMapper's own types: a type name, then a type-specific payload."""
        name = self.qbytearray()
        payload_start = self.pos
        if name == b"MadColor\x00":
            self.qstring()  # color spec, e.g. "rgb"
            self.take(4 * 8)
        elif name == b"QVector<float>\x00":
            self.take(8 * self.u32())
        elif name == b"FilePath\x00":
            self.qstring()
        else:
            raise MadFileError(f"unsupported user type {name!r} at offset {start}")
        return (name, self.data[payload_start:self.pos])


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray()

    def u32(self, value: int) -> None:
        self.out += struct.pack(">I", value)

    def qstring(self, text: str | None) -> None:
        if text is None:
            self.u32(0xFFFFFFFF)
            return
        encoded = text.encode("utf-16-be")
        self.u32(len(encoded))
        self.out += encoded

    def qbytearray(self, data: bytes | None) -> None:
        if data is None:
            self.u32(0xFFFFFFFF)
            return
        self.u32(len(data))
        self.out += data

    def variant_map(self, pairs: VariantMap) -> None:
        self.u32(len(pairs))
        for key, item in pairs:
            self.qstring(key)
            self.variant(item)

    def variant(self, var: Variant) -> None:
        type_id, value, is_null = var
        self.u32(type_id)
        self.out.append(is_null)
        if type_id in (T_INVALID, T_STRING):
            self.qstring(value)
        elif type_id == T_BOOL:
            self.out.append(1 if value else 0)
        elif type_id in _STRUCTS:
            values = value if type_id in _TUPLE_TYPES else (value,)
            self.out += struct.pack(_STRUCTS[type_id], *values)
        elif type_id in (T_BYTEARRAY, T_JSONDOC):
            self.qbytearray(value)
        elif type_id == T_STRINGLIST:
            self.u32(len(value))
            for text in value:
                self.qstring(text)
        elif type_id == T_LIST:
            self.u32(len(value))
            for item in value:
                self.variant(item)
        elif type_id == T_MAP:
            self.variant_map(value)
        elif type_id in _FIXED_SIZE or type_id == T_IMAGE:
            self.out += value
        elif type_id == T_USER:
            name, payload = value
            self.qbytearray(name)
            self.out += payload
        else:
            raise MadFileError(f"unsupported value type {type_id}")


class MadProject:
    """A loaded .mad file: its stream version and top-level map."""

    def __init__(self, version: int, root: VariantMap) -> None:
        self.version = version
        self.root = root

    @classmethod
    def from_bytes(cls, data: bytes) -> "MadProject":
        reader = _Reader(data)
        if reader.u32() != MAGIC:
            raise MadFileError("not a MadMapper project file")
        version = reader.u32()
        root = reader.variant_map()
        if reader.pos != len(data):
            raise MadFileError(f"{len(data) - reader.pos} unread bytes at end of file")
        return cls(version, root)

    @classmethod
    def load(cls, path: Path) -> "MadProject":
        return cls.from_bytes(Path(path).read_bytes())

    def to_bytes(self) -> bytes:
        writer = _Writer()
        writer.u32(MAGIC)
        writer.u32(self.version)
        writer.variant_map(self.root)
        return bytes(writer.out)

    def save(self, path: Path) -> None:
        Path(path).write_bytes(self.to_bytes())


def get(pairs: VariantMap, key: str) -> Variant | None:
    """The variant stored under `key` in a decoded map, or None."""
    for existing_key, item in pairs:
        if existing_key == key:
            return item
    return None


def set_value(pairs: VariantMap, key: str, var: Variant) -> None:
    """Replace the variant under `key`, or append it if the map lacks it."""
    for i, (existing_key, _) in enumerate(pairs):
        if existing_key == key:
            pairs[i] = (key, var)
            return
    pairs.append((key, var))


def to_plain(var: Variant) -> Any:
    """A decoded variant as plain Python, for reading values out of it."""
    type_id, value, _ = var
    if type_id == T_MAP:
        return {key: to_plain(item) for key, item in value}
    if type_id == T_LIST:
        return [to_plain(item) for item in value]
    return value
