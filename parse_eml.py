#!/usr/bin/env python3
"""
parse_eml.py

Parse a .eml file and extract:
 - decoded headers (From, To, Subject, Date, and all headers)
 - text/plain and text/html bodies (first occurrences)
 - attachments (save to disk if requested) with metadata

Usage:
  python parse_eml.py input.eml
  python parse_eml.py input.eml --outdir ./extracted --no-save-attachments
  python parse_eml.py input.eml --json-only

Requirements: Python 3.8+ (uses only stdlib)
"""
from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
from email import policy
from email.parser import BytesParser
from email.header import decode_header
from typing import Optional, List, Dict, Any

def decode_header_value(value: Optional[str]) -> str:
    """Decode an RFC2047 encoded header to a Python string."""
    if not value:
        return ""
    parts = decode_header(value)
    out_parts: List[str] = []
    for bytes_or_str, encoding in parts:
        if isinstance(bytes_or_str, str):
            out_parts.append(bytes_or_str)
        else:
            enc = encoding if encoding is not None else "utf-8"
            try:
                out_parts.append(bytes_or_str.decode(enc, errors="replace"))
            except Exception:
                out_parts.append(bytes_or_str.decode("utf-8", errors="replace"))
    return "".join(out_parts)

def make_safe_filename(name: str) -> str:
    """Sanitize filename to avoid path traversal and remove problematic chars."""
    # Remove directory components
    name = Path(name).name
    # Replace whitespace and dangerous chars
    name = re.sub(r"[\\\/\0]", "_", name)
    # Optionally keep unicode, but remove control chars
    name = "".join(ch for ch in name if ord(ch) > 31)
    if not name:
        name = "unknown"
    return name

def unique_path_for(path: Path) -> Path:
    """If path exists, append a counter to make it unique."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def save_bytes_to_file(b: bytes, outdir: Path, filename: str) -> Path:
    safe = make_safe_filename(filename)
    outpath = unique_path_for(outdir / safe)
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_bytes(b)
    return outpath

def get_filename_from_part(part) -> Optional[str]:
    # email.message.EmailMessage.get_filename() should handle RFC2231,
    # but we decode it again just in case.
    raw_name = part.get_filename()
    if raw_name:
        return decode_header_value(raw_name)
    # try Content-Disposition params
    try:
        cd = part.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\'|["\']?)([^"\';\n]+)', cd, flags=re.IGNORECASE)
        if m:
            return decode_header_value(m.group(1))
    except Exception:
        pass
    return None

def bytes_to_text(b: bytes, charset: Optional[str]) -> str:
    if b is None:
        return ""
    # Try given charset, then common fallbacks
    tried = []
    if charset:
        tried.append(charset)
    tried.extend(["utf-8", "latin-1", "cp1252"])
    for enc in tried:
        try:
            return b.decode(enc, errors="replace")
        except Exception:
            continue
    # as last resort
    return b.decode("utf-8", errors="replace")

def parse_eml(path: Path, save_attachments: bool = True, outdir: Optional[Path] = None) -> Dict[str, Any]:
    raw = path.read_bytes()
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    # Headers
    headers: Dict[str, str] = {}
    for k in msg.keys():
        headers[k] = decode_header_value(msg.get(k))

    # Bodies and attachments
    text_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    for part in msg.walk():
        if part.is_multipart():
            continue  # containers only
        ctype = part.get_content_type()
        cdisp = part.get_content_disposition()  # 'inline', 'attachment', or None
        filename = get_filename_from_part(part)

        payload_bytes = part.get_payload(decode=True)  # returns bytes or None
        charset = part.get_content_charset()

        # Heuristics: treat text/plain and text/html as bodies unless filename indicates attachment
        if ctype == "text/plain" and filename is None:
            txt = bytes_to_text(payload_bytes or b"", charset)
            text_parts.append(txt)
            continue
        if ctype == "text/html" and filename is None:
            html = bytes_to_text(payload_bytes or b"", charset)
            html_parts.append(html)
            continue

        # Otherwise treat as attachment (images, application/*, or parts with filename)
        if filename or cdisp == "attachment" or ctype.startswith("image/") or ctype.startswith("application/"):
            attach_info: Dict[str, Any] = {
                "content_type": ctype,
                "content_disposition": cdisp,
                "filename": filename or "",
                "size": len(payload_bytes) if payload_bytes is not None else 0,
            }
            if save_attachments and outdir is not None and payload_bytes:
                save_name = filename or f"part-{len(attachments)+1}"
                saved_path = save_bytes_to_file(payload_bytes, outdir, save_name)
                attach_info["saved_path"] = str(saved_path)
            attachments.append(attach_info)
            continue

        # Fallback: try to decode text
        if payload_bytes:
            maybe_text = bytes_to_text(payload_bytes, charset)
            # choose where to put it - prefer text
            text_parts.append(maybe_text)

    result: Dict[str, Any] = {
        "file": str(path),
        "headers": headers,
        "text": text_parts[0] if text_parts else "",
        "all_text_parts_count": len(text_parts),
        "html": html_parts[0] if html_parts else "",
        "all_html_parts_count": len(html_parts),
        "attachments": attachments,
    }
    return result

def main():
    p = argparse.ArgumentParser(description="Parse a .eml file and extract headers, bodies, and attachments.")
    p.add_argument("eml", type=Path, help=".eml file to parse")
    p.add_argument("--outdir", type=Path, default=Path("./extracted"), help="Directory to save attachments (default: ./extracted)")
    p.add_argument("--no-save-attachments", dest="save_attachments", action="store_false", help="Do not save attachments to disk")
    p.add_argument("--json-only", action="store_true", help="Only print JSON summary (no extra stdout)")
    args = p.parse_args()

    if not args.eml.exists():
        raise SystemExit(f"File not found: {args.eml}")

    outdir = args.outdir.resolve() if args.save_attachments else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    summary = parse_eml(args.eml, save_attachments=args.save_attachments, outdir=outdir)

    if args.json_only:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    # Human-friendly summary
    print("Parsed:", summary["file"])
    hdrs = summary["headers"]
    print("From:", hdrs.get("From", ""))
    print("To:", hdrs.get("To", ""))
    print("Subject:", hdrs.get("Subject", ""))
    print("Date:", hdrs.get("Date", ""))
    print()
    if summary["text"]:
        print("--- text/plain (first part) ---")
        print(summary["text"][:1000] + ("..." if len(summary["text"]) > 1000 else ""))
        print()
    if summary["html"]:
        print("--- text/html (first part) ---")
        print(summary["html"][:1000] + ("..." if len(summary["html"]) > 1000 else ""))
        print()

    if summary["attachments"]:
        print("Attachments:")
        for a in summary["attachments"]:
            print(f" - filename={a.get('filename')!r} type={a.get('content_type')} size={a.get('size')}")
            if "saved_path" in a:
                print(f"   saved to: {a['saved_path']}")
    else:
        print("No attachments found.")

    # Print JSON summary at the end for programmatic consumption
    print()
    print("JSON summary:")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()