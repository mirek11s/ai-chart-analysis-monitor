from typing import Protocol


class VoiceIO(Protocol):
    """Optional adapter contract; no microphone or cloud service used by default."""

    def listen(self) -> str: ...
    def speak(self, text: str) -> None: ...
