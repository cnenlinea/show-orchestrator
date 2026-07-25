
import mido
import time

print(mido.get_input_names())
with mido.open_input("BitFocus 1") as inport:
    for msg in inport:
        print(msg)