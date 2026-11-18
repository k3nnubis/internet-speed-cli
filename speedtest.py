#!/usr/bin/env python3
"""Measure download speed with ten sequential HTTP downloads (stdlib only)."""

import argparse
from dataclasses import dataclass
from http.client import HTTPException, IncompleteRead
import math
import sys
from time import perf_counter
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class Measurement:
    seconds: float
    size_bytes: int


def http_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("expected an http:// or https:// URL with a host")
        parts.port  # Validate an explicitly supplied port.
        if any(character.isspace() for character in value):
            raise ValueError("URL must not contain whitespace; use percent-encoding")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return seconds


def download(url: str, timeout: float) -> Measurement:
    request = Request(url, headers={
        "User-Agent": "internet-speed-cli/1.0",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    size_bytes = 0
    started = perf_counter()
    with urlopen(request, timeout=timeout) as response:
        while chunk := response.read(CHUNK_SIZE):
            size_bytes += len(chunk)
        # Sized reads may silently reach EOF before Content-Length is satisfied.
        if response.length is not None and response.length > 0:
            raise IncompleteRead(b"", response.length)
    return Measurement(perf_counter() - started, size_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=http_url, help="HTTP(S) URL of a file to download")
    parser.add_argument(
        "--timeout", type=positive_seconds, default=30.0,
        help="socket operation timeout in seconds (default: 30)",
    )
    args = parser.parse_args(argv)
    measurements = []
    for number in range(1, REQUEST_COUNT + 1):
        try:
            result = download(args.url, args.timeout)
        except (URLError, OSError, HTTPException, ValueError, UnicodeError) as error:
            print(f"Request {number}/{REQUEST_COUNT} failed: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        measurements.append(result)
        print(f"[{number}/{REQUEST_COUNT}] {result.size_bytes} bytes in {result.seconds:.6f} s", flush=True)

    total_seconds = sum(result.seconds for result in measurements)
    total_bytes = sum(result.size_bytes for result in measurements)
    bytes_per_second = total_bytes / total_seconds
    print(f"\nAverage request time: {total_seconds / REQUEST_COUNT:.6f} s")
    print(f"Total downloaded: {total_bytes} bytes ({total_bytes / 1024**2:.3f} MiB)")
    print(f"Download speed: {bytes_per_second / 1024**2:.3f} MiB/s ({bytes_per_second * 8 / 1_000_000:.3f} Mbit/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
