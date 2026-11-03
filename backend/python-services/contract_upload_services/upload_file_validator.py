import os
import io
from dataclasses import dataclass
from typing import Optional

from fastapi import UploadFile

from contract_upload_services.constants import (
    MAX_CONTRACT_UPLOAD_BYTES,
    MIN_CONTRACT_WORDS,
    CONTRACT_UPLOAD_ERROR_MESSAGES,
)


@dataclass
class UploadContractValidationError(Exception):
    code: str
    message: str

    def to_detail(self) -> dict:
        return {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }


def _code_msg(code: str, **fill) -> tuple[str, str]:
    msg = CONTRACT_UPLOAD_ERROR_MESSAGES.get(code)
    if not msg:
        # Fallback: should never happen
        return code, "Invalid contract upload."
    return code, (msg.format(**fill) if fill else msg)


def count_words(text: str) -> int:
    """Words as a reader would count them.

    A word is a whitespace-separated run with at least one letter or digit in
    it. So "policy/inspection" is one word and "10,000" is one, while a bare
    dash, bullet or "§" is none — punctuation on its own line would otherwise
    let a page of rules and numbering pass for a contract. Unicode-aware, so a
    wording in another script counts the same way.
    """
    return sum(1 for tok in (text or "").split() if any(ch.isalnum() for ch in tok))


def _get_ext_lower(uploaded_filename: Optional[str]) -> str:
    if not uploaded_filename:
        return ""
    return os.path.splitext(uploaded_filename)[1].lower()


def _sniff_pdf_magic(file_bytes: bytes) -> bool:
    # PDF files start with: %PDF-...
    return file_bytes[:5] == b"%PDF-"


def _sniff_docx_magic(file_bytes: bytes) -> bool:
    # DOCX is a zip archive; common first bytes: PK\x03\x04
    return file_bytes[:4] == b"PK\x03\x04"


def validate_uploaded_contract(
    *,
    file: UploadFile,
    file_bytes: bytes,
    temp_file_path: Optional[str] = None,
) -> None:
    """Validate an uploaded contract file.

    Raises UploadContractValidationError on failure.

    Note: we validate INBOUND format using extension + magic bytes.
    Content-based validations (encrypted, corrupt, insufficient text)
    run using existing extractors.
    """

    if file_bytes is None:
        raise UploadContractValidationError(
            *_code_msg("CORRUPT_FILE")
        )

    # 1) SIZE — an upper bound only. How SHORT a contract may be is decided by
    # its words, further down, once it has been read.
    size = len(file_bytes)
    if size > MAX_CONTRACT_UPLOAD_BYTES:
        raise UploadContractValidationError(*_code_msg("FILE_TOO_LARGE"))

    if size == 0:
        # Nothing to read at all: that is a broken file, not a short contract.
        raise UploadContractValidationError(*_code_msg("CORRUPT_FILE"))

    # 2) FORMAT
    ext = _get_ext_lower(file.filename)

    # Allowed: PDF and DOCX only
    if ext not in [".pdf", ".docx"]:
        raise UploadContractValidationError(*_code_msg("INVALID_FORMAT"))

    if ext == ".pdf" and not _sniff_pdf_magic(file_bytes):
        raise UploadContractValidationError(*_code_msg("INVALID_FORMAT"))

    if ext == ".docx" and not _sniff_docx_magic(file_bytes):
        raise UploadContractValidationError(*_code_msg("INVALID_FORMAT"))

    # 3) ENCRYPTED PDF
    # Only for PDFs. We rely on PyPDF2 being available in requirements.
    if ext == ".pdf":
        try:
            from PyPDF2 import PdfReader

            pdf_stream = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_stream)
            if getattr(reader, "is_encrypted", False):
                # `is_encrypted` is True for ANY PDF carrying an encryption
                # dictionary — including permissions-only PDFs that have an
                # empty user password and open with no prompt (common from
                # Word/DocuSign/Adobe exports). Only reject when the file
                # genuinely cannot be opened without a password: try decrypting
                # with an empty password first (PyPDF2 returns 0 on failure).
                try:
                    needs_password = reader.decrypt("") == 0
                except Exception:
                    needs_password = True
                if needs_password:
                    # Truly password-protected — instruct user to remove it.
                    raise UploadContractValidationError(*_code_msg("ENCRYPTED_PDF"))
        except UploadContractValidationError:
            raise
        except Exception:
            # If we can't inspect encryption status, treat as corrupt
            raise UploadContractValidationError(*_code_msg("CORRUPT_FILE"))

    # 4) CORRUPT FILE + 5) INSUFFICIENT TEXT
    # Use existing extract_document_data + attempt to measure readable text.
    if not temp_file_path:
        raise UploadContractValidationError(*_code_msg("CORRUPT_FILE"))

    try:
        from contract_upload_services.document_extractors import extract_document_data

        extracted = extract_document_data(temp_file_path)

        # Measure readable text
        readable_text = ""
        if extracted.get("type") == "pdf":
            # pages: [{page, text, blocks}]
            readable_text = "\n".join(
                (p.get("text") or "") for p in (extracted.get("pages") or [])
            )
        elif extracted.get("type") == "docx":
            # data: [{text, tables}]
            data = extracted.get("data") or []
            if data and isinstance(data, list):
                readable_text = "\n".join((d.get("text") or "") for d in data)
        else:
            # Should not happen because we already enforced format by extension/magic,
            # but keep safe.
            raise UploadContractValidationError(*_code_msg("INVALID_FORMAT"))

        # Clean/normalize for a stable threshold
        readable_text = readable_text.replace("\u00a0", " ")
        readable_text = "\n".join(
            line.strip() for line in readable_text.splitlines() if line.strip()
        )
        words = count_words(readable_text)

        # NO WORDS AT ALL is a different problem from too few, and it gets the
        # advice that fixes it. The extractor already OCRs image-only pages when
        # Tesseract is installed, so a document that still yields nothing is a
        # scan it could not read — telling someone with twenty scanned pages
        # that their contract is "too short" would send them the wrong way.
        if words == 0:
            raise UploadContractValidationError(*_code_msg("INSUFFICIENT_TEXT"))

        if words < MIN_CONTRACT_WORDS:
            raise UploadContractValidationError(*_code_msg(
                "CONTRACT_TOO_SHORT", words=words, min_words=MIN_CONTRACT_WORDS))

    except UploadContractValidationError:
        raise
    except UploadContractValidationError as exc:
        raise exc
    except Exception:
        raise UploadContractValidationError(*_code_msg("CORRUPT_FILE"))

