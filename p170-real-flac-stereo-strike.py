#!/usr/bin/env python3
import argparse
import array
import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MIN_SIDE_ENERGY_RATIO = 1e-6

CANDIDATES = [
    {
        "id": "wikimedia-postcard-piano-2026",
        "title": "Postcardwithaballoon-tobefree.flac",
        "page_url": "https://commons.wikimedia.org/wiki/File:Postcardwithaballoon-tobefree.flac",
        "description": "Audio recording of 'Postcard with a Balloon', a piano song, played by its composer",
        "source_class": "REAL_PERFORMANCE_RECORDING",
    },
    {
        "id": "wikimedia-mode-de-re-2015",
        "title": "Mode de ré.flac",
        "page_url": "https://commons.wikimedia.org/wiki/File:Mode_de_r%C3%A9.flac",
        "description": "Own-work musical recording: Mode de ré",
        "source_class": "REAL_AUDIO_RECORDING",
    },
    {
        "id": "wikimedia-aregadegadeng-2024",
        "title": "Aregadegadeng.flac",
        "page_url": "https://commons.wikimedia.org/wiki/File:Aregadegadeng.flac",
        "description": "A tune of the Pasyon used in the Tagalog region",
        "source_class": "REAL_AUDIO_RECORDING",
    },
    {
        "id": "wikimedia-ambient-2021",
        "title": "Raspberrymusic - Ambient (10 minutes).flac",
        "page_url": "https://commons.wikimedia.org/wiki/File:Raspberrymusic_-_Ambient_(10_minutes).flac",
        "description": "FLAC file of ambient music",
        "source_class": "REAL_MUSIC_AUDIO",
    },
]


def run(cmd, *, input_bytes=None):
    return subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def download_original(candidate, target):
    quoted = urllib.parse.quote(candidate["title"], safe="")
    url = f"https://commons.wikimedia.org/wiki/Special:Redirect/file/{quoted}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Laut-P170-real-corpus-strike/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        final_url = resp.geturl()
        content_type = resp.headers.get("Content-Type")
        with open(target, "wb") as out:
            sha = hashlib.sha256()
            total = 0
            first = b""
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                if len(first) < 4:
                    first += chunk[: 4 - len(first)]
                out.write(chunk)
                sha.update(chunk)
                total += len(chunk)
    return {
        "download_url": url,
        "final_url": final_url,
        "content_type": content_type,
        "sha256": sha.hexdigest(),
        "bytes": total,
        "magic_hex": first[:4].hex(),
        "magic_ascii": first[:4].decode("ascii", errors="replace"),
    }


def probe(path):
    proc = run([
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=format_name,duration,size,bit_rate:stream=index,codec_name,codec_long_name,codec_type,sample_fmt,sample_rate,channels,channel_layout,bits_per_sample,bits_per_raw_sample",
        "-of", "json",
        str(path),
    ])
    data = json.loads(proc.stdout.decode("utf-8"))
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise RuntimeError(f"expected exactly one audio stream, got {len(audio)}")
    return data.get("format", {}), audio[0]


def decode_s32le(path, channels):
    proc = run([
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(path), "-map", "0:a:0",
        "-f", "s32le", "-acodec", "pcm_s32le", "pipe:1",
    ])
    samples = array.array("i")
    samples.frombytes(proc.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) % channels:
        raise RuntimeError("decoded sample count is not divisible by channel count")
    return samples


def stereo_metrics(samples):
    n = len(samples) // 2
    if n == 0:
        raise RuntimeError("zero decoded stereo frames")

    sx = sy = sxx = syy = sxy = 0
    mid_energy = side_energy = diff_energy = lr_energy = 0
    equal = True
    differing_frames = 0

    for i in range(0, len(samples), 2):
        x = int(samples[i])
        y = int(samples[i + 1])
        if x != y:
            equal = False
            differing_frames += 1
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
        sm = x + y
        sd = x - y
        mid_energy += sm * sm
        side_energy += sd * sd
        diff_energy += sd * sd
        lr_energy += x * x + y * y

    num = n * sxy - sx * sy
    den_x = n * sxx - sx * sx
    den_y = n * syy - sy * sy
    corr = None
    if den_x > 0 and den_y > 0:
        corr = num / math.sqrt(den_x * den_y)

    total_ms = mid_energy + side_energy
    side_ratio = side_energy / total_ms if total_ms > 0 else None
    diff_ratio = diff_energy / lr_energy if lr_energy > 0 else None

    return {
        "decoded_frames": n,
        "left_right_identical": equal,
        "differing_frames": differing_frames,
        "differing_frame_fraction": differing_frames / n,
        "channel_correlation": corr,
        "mid_energy_integer": str(mid_energy),
        "side_energy_integer": str(side_energy),
        "lr_difference_energy_integer": str(diff_energy),
        "lr_total_energy_integer": str(lr_energy),
        "side_energy_ratio": side_ratio,
        "lr_difference_energy_ratio": diff_ratio,
        "nonzero_lr_difference_energy": diff_energy > 0,
    }


def analyze_candidate(candidate, workdir):
    path = workdir / (candidate["id"] + ".flac")
    physical = download_original(candidate, path)
    format_info, stream = probe(path)

    codec = stream.get("codec_name")
    format_name = format_info.get("format_name", "")
    channels = int(stream.get("channels", 0))
    source_flac = (
        physical["magic_ascii"] == "fLaC"
        and codec == "flac"
        and "flac" in format_name.split(",")
        and physical["bytes"] > 0
        and "upload.wikimedia.org" in physical["final_url"]
    )

    result = {
        "candidate": candidate,
        "physical": physical,
        "format": format_info,
        "stream": stream,
        "flac_source_bytes_pass": source_flac,
        "stereo": None,
        "genuine_stereo_pass": False,
        "reasons": [],
    }

    if not source_flac:
        result["reasons"].append("source bytes did not validate as direct Wikimedia native FLAC")
        return result
    if channels != 2:
        result["reasons"].append(f"channels={channels}, requires exactly 2 for this strike")
        return result

    pcm = decode_s32le(path, channels)
    metrics = stereo_metrics(pcm)
    result["stereo"] = metrics

    corr_finite = metrics["channel_correlation"] is not None and math.isfinite(metrics["channel_correlation"])
    side_material = metrics["side_energy_ratio"] is not None and metrics["side_energy_ratio"] >= MIN_SIDE_ENERGY_RATIO
    genuine = (
        not metrics["left_right_identical"]
        and metrics["nonzero_lr_difference_energy"]
        and side_material
        and corr_finite
    )
    result["genuine_stereo_pass"] = genuine
    if metrics["left_right_identical"]:
        result["reasons"].append("L/R samples are exactly equal")
    if not metrics["nonzero_lr_difference_energy"]:
        result["reasons"].append("L-R energy is zero")
    if not side_material:
        result["reasons"].append(
            f"side_energy_ratio below materiality floor {MIN_SIDE_ENERGY_RATIO}"
        )
    if not corr_finite:
        result["reasons"].append("channel correlation undefined/non-finite")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="p170-real-flac-stereo.evidence.json")
    args = parser.parse_args()

    workdir = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "p170-real-flac-stereo"
    workdir.mkdir(parents=True, exist_ok=True)

    evidence = {
        "mission": "P170",
        "measurement_version": "laut-p170-real-flac-stereo-v1",
        "material_side_energy_ratio_floor": MIN_SIDE_ENERGY_RATIO,
        "source_rule": "original remote .flac physical bytes only; no local WAV->FLAC conversion",
        "producer_base_sha": os.environ.get("P170_PRODUCER_BASE_SHA"),
        "candidate_branch_sha": os.environ.get("GITHUB_SHA"),
        "runner": {
            "os": os.environ.get("RUNNER_OS"),
            "arch": os.environ.get("RUNNER_ARCH"),
        },
        "candidates": [],
        "selected": None,
        "FLAC_AXIS": "BLOCKED",
        "GENUINE_STEREO_AXIS": "BLOCKED",
    }

    for candidate in CANDIDATES:
        print(f"P170 candidate: {candidate['id']} {candidate['title']}", flush=True)
        try:
            result = analyze_candidate(candidate, workdir)
        except Exception as exc:
            result = {
                "candidate": candidate,
                "flac_source_bytes_pass": False,
                "genuine_stereo_pass": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        evidence["candidates"].append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if result.get("flac_source_bytes_pass") and result.get("genuine_stereo_pass"):
            evidence["selected"] = result
            evidence["FLAC_AXIS"] = "PASS"
            evidence["GENUINE_STEREO_AXIS"] = "PASS"
            break

    Path(args.output).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("P170_FINAL=" + json.dumps({
        "FLAC_AXIS": evidence["FLAC_AXIS"],
        "GENUINE_STEREO_AXIS": evidence["GENUINE_STEREO_AXIS"],
        "selected_id": evidence["selected"]["candidate"]["id"] if evidence["selected"] else None,
        "sha256": evidence["selected"]["physical"]["sha256"] if evidence["selected"] else None,
    }), flush=True)

    if evidence["FLAC_AXIS"] != "PASS" or evidence["GENUINE_STEREO_AXIS"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
