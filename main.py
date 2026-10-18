#!/usr/bin/env python3
"""USM -> MP4 converter.

Run:  python3 main.py

Works on a single .usm file or on a whole folder of .usm files.
The video is copied into an MP4 container without re-encoding (no quality loss).
Supports both H.264 and VP9 video inside the .usm. Audio is not included.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


class ConvertError(Exception):
    """An error with a human readable reason and a suggested fix."""

    def __init__(self, reason, fix):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


# --------------------------------------------------------------------------
# input helpers
# --------------------------------------------------------------------------

def clean_path(raw):
    """Strip whitespace/quotes that appear when a path is dragged into a terminal."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if os.name != "nt":
        text = text.replace("\\ ", " ")
    return os.path.expanduser(text.strip())


def ask(prompt, required):
    while True:
        value = input(prompt).strip()
        if value:
            return value
        if not required:
            return ""
        print("error! this field is required. type a value and press enter.")


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------

def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise ConvertError(
            "ffmpeg was not found.",
            "run: pip install -r requirements.txt (or install ffmpeg and add it to PATH).",
        )


def run_ffmpeg(ffmpeg, source, dest):
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    if not ok:
        if dest.exists():
            dest.unlink()
        lines = [ln.strip() for ln in result.stderr.splitlines() if ln.strip()]
        detail = lines[-1] if lines else "unknown ffmpeg error"
        raise ConvertError(
            "ffmpeg could not convert the file (" + detail + ").",
            "the file may be damaged or encrypted. try a fresh copy of the .usm file.",
        )


# --------------------------------------------------------------------------
# USM parsing
# --------------------------------------------------------------------------

def iter_video_payloads(path):
    """Yield the raw video data of every video chunk in a .usm file."""
    cut_off = ConvertError(
        "the .usm file is cut off (incomplete).",
        "copy or download the file again and make sure it finished.",
    )
    with open(path, "rb") as f:
        if f.read(4) != b"CRID":
            raise ConvertError(
                "this is not a valid .usm file (missing CRID header).",
                "pick a real .usm file. it may be corrupted or another format.",
            )
        f.seek(0)
        while True:
            head = f.read(8)
            if not head:
                break
            if len(head) < 8:
                raise cut_off
            sig = head[:4]
            size = struct.unpack(">I", head[4:])[0]
            body = f.read(size)
            if len(body) < size:
                raise cut_off
            if sig != b"@SFV" or size < 16:
                continue
            offset = body[1]
            padding = struct.unpack(">H", body[2:4])[0]
            payload_type = body[7] & 0x3
            end = len(body) - padding
            if payload_type != 0 or offset > end:
                continue
            yield body[offset:end]


def convert_one(ffmpeg, source, dest):
    gen = iter_video_payloads(source)
    first = next(gen, None)
    gen.close()
    if first is None:
        raise ConvertError(
            "no video stream was found in this .usm file.",
            "it may be an audio-only .usm. pick the video one (usually pv_xxx.usm).",
        )

    if first[:4] == b"DKIF":
        # VP9 video stored as an IVF stream: extract it, then wrap it in MP4.
        with tempfile.TemporaryDirectory() as tmp:
            ivf = Path(tmp) / "video.ivf"
            with open(ivf, "wb") as out:
                for payload in iter_video_payloads(source):
                    out.write(payload)
            run_ffmpeg(ffmpeg, ivf, dest)
    else:
        # H.264 video: ffmpeg can read it straight from the .usm.
        run_ffmpeg(ffmpeg, source, dest)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def collect_sources(source):
    if source.is_file():
        if source.suffix.lower() != ".usm":
            raise ConvertError(
                "that file is not a .usm file.",
                "pick a file ending in .usm, or a folder that contains .usm files.",
            )
        return [source]
    if source.is_dir():
        files = sorted(
            p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".usm"
        )
        if not files:
            raise ConvertError(
                "no .usm files were found in that folder.",
                "check the folder path. only files directly inside it are used, not subfolders.",
            )
        return files
    raise ConvertError(
        "that location does not exist: " + str(source),
        "check the path for typos. you can drag the file or folder into the terminal.",
    )


def make_output_name(name, src, index, batch):
    if not name:
        return src.stem + ".mp4"
    base = name[:-4] if name.lower().endswith(".mp4") else name
    if batch:
        base = "{}_{}".format(base, index)
    return base + ".mp4"


def main():
    print()
    source_in = ask(".usm file location (required) : ", True)
    output_in = ask("output folder (required) : ", True)
    name = ask("name: ", False)

    try:
        source = Path(clean_path(source_in))
        files = collect_sources(source)

        if name and any(sep in name for sep in ("/", "\\")):
            raise ConvertError(
                "the name cannot contain slashes.",
                "type just a file name, for example: my video",
            )

        out_dir = Path(clean_path(output_in))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConvertError(
                "could not use the output folder (" + str(exc) + ").",
                "choose a folder you are allowed to write to.",
            )

        ffmpeg = find_ffmpeg()
    except ConvertError as err:
        print("error! {} {}".format(err.reason, err.fix))
        return 1

    batch = len(files) > 1
    failed = 0
    for i, src in enumerate(files, start=1):
        dest = out_dir / make_output_name(name, src, i, batch)
        try:
            print("converting {} ...".format(src.name))
            convert_one(ffmpeg, src, dest)
        except ConvertError as err:
            failed += 1
            print("error! {}: {} {}".format(src.name, err.reason, err.fix))
        except Exception as exc:  # unexpected problem
            failed += 1
            print(
                "error! {}: unexpected problem ({}). "
                "try again, or try a different copy of the file.".format(src.name, exc)
            )

    if failed:
        return 1
    print("done!")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nerror! cancelled. run the program again when you are ready.")
        sys.exit(1)
