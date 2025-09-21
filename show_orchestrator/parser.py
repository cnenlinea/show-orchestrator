import yaml

from show_orchestrator.models import Show


class Parser:
    def __init__(self) -> None:
        self.show = None

    def load_show_from_yaml(self, file_path: str) -> Show:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = yaml.safe_load(file)
            self.show = Show(**data)
        return self.show
