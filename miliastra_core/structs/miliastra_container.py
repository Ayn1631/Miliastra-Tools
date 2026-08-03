from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from miliastra_core.protobuf.wire import WireField, parse_fields, rebuild_message


HEADER_SIZE = 20
FOOTER_SIZE = 4


class ContainerError(ValueError):
    pass


@dataclass(frozen=True)
class ContainerHeader:
    left_size: int
    schema: int
    head_tag: int
    file_type: int
    payload_size: int


@dataclass(frozen=True)
class MiliastraContainer:
    header: ContainerHeader
    payload: bytes
    footer: bytes
    source_path: Path | None = None

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        source_path: Path | None = None,
        strict: bool = True,
    ) -> "MiliastraContainer":
        if len(data) < HEADER_SIZE + FOOTER_SIZE:
            raise ContainerError("file is smaller than the 24-byte container overhead")
        header = ContainerHeader(*struct.unpack(">IIIII", data[:HEADER_SIZE]))
        payload = data[HEADER_SIZE:-FOOTER_SIZE]
        if strict:
            if header.left_size != len(data) - FOOTER_SIZE:
                raise ContainerError(
                    f"left_size={header.left_size}, expected {len(data) - FOOTER_SIZE}"
                )
            if header.payload_size != len(payload):
                raise ContainerError(
                    f"payload_size={header.payload_size}, expected {len(payload)}"
                )
        return cls(header, payload, data[-FOOTER_SIZE:], source_path)

    @classmethod
    def load(cls, path: str | Path, *, strict: bool = True) -> "MiliastraContainer":
        source = Path(path)
        return cls.from_bytes(source.read_bytes(), source_path=source, strict=strict)

    def top_fields(self) -> list[WireField]:
        return parse_fields(self.payload, context="Miliastra payload")

    def with_top_fields(self, fields: list[WireField]) -> "MiliastraContainer":
        payload = rebuild_message(fields)
        header = ContainerHeader(
            len(payload) + HEADER_SIZE,
            self.header.schema,
            self.header.head_tag,
            self.header.file_type,
            len(payload),
        )
        return MiliastraContainer(header, payload, self.footer, self.source_path)

    def build_bytes(self) -> bytes:
        header = struct.pack(
            ">IIIII",
            len(self.payload) + HEADER_SIZE,
            self.header.schema,
            self.header.head_tag,
            self.header.file_type,
            len(self.payload),
        )
        return header + self.payload + self.footer

    def validate_roundtrip(self) -> None:
        if rebuild_message(self.top_fields()) != self.payload:
            raise ContainerError("lossless wire roundtrip changed the payload")
        rebuilt = self.build_bytes()
        if MiliastraContainer.from_bytes(rebuilt).build_bytes() != rebuilt:
            raise ContainerError("container roundtrip changed bytes")
