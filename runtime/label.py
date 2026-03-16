"""CJanus label management for goto, come-from, begin, and end statements."""


class Labels:
    def __init__(self):
        self._gotomap: dict[str, int] = {}
        self._comemap: dict[str, int] = {}
        self._beginmap: dict[str, int] = {}
        self._endmap: dict[str, int] = {}
        self._tmplabel: int = 0

    def get_goto(self, name: str) -> int:
        if name in self._gotomap:
            return self._gotomap[name]
        raise KeyError(f"goto label not found: {name}")

    def get_come_from(self, name: str) -> int:
        if name in self._comemap:
            return self._comemap[name]
        raise KeyError(f"come-from label not found: {name}")

    def get_begin(self, name: str) -> int:
        if name in self._beginmap:
            return self._beginmap[name]
        raise KeyError(f"begin label not found: {name}")

    def get_end(self, name: str) -> int:
        if name in self._endmap:
            return self._endmap[name]
        raise KeyError(f"end label not found: {name}")

    def add_goto(self, name: str, lineno: int) -> None:
        self._gotomap[name] = lineno

    def add_come_from(self, name: str, lineno: int) -> None:
        self._comemap[name] = lineno

    def add_begin(self, name: str, lineno: int) -> None:
        self._beginmap[name] = lineno

    def add_end(self, name: str, lineno: int) -> None:
        self._endmap[name] = lineno

    def register_new_label(self, lineno: int) -> str:
        """Creates a temporary label pair for implicit blocks."""
        s = f"_{self._tmplabel}"
        self.add_goto(s, lineno)
        self.add_come_from(s, lineno + 1)
        self._tmplabel += 1
        return s
