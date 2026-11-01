from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.intake.id_extract import IdImageError, _IdOut, extract_id, postprocess, prepare_images, verhoeff_valid
from app.models import IdType
from tests.fakes import FakeLLM

# 999941057058 is a Verhoeff-valid demo number; changing a digit breaks it.
VALID_AADHAAR = "999941057058"


def jpeg(size=(2400, 1500), exif_rotate=False) -> bytes:
    img = Image.new("RGB", size, (200, 210, 220))
    buf = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def v(value, conf=0.95):
    return {"value": value, "confidence": conf}


def none():
    return {"value": None, "confidence": 0}


def aadhaar_out(**over):
    out = {
        "is_identity_document": True, "document_type": "aadhaar", "legible": True, "issues": [],
        "name": v("PRIYA SHARMA", 0.97), "dob": v("2004-08-15", 0.93), "year_of_birth": none(),
        "gender": v("female", 0.98), "id_number": v("9999 4105 7058", 0.92),
        "father_or_spouse_name": v("Rakesh Sharma", 0.9),
        "address_line1": v("House 12, Gandhi Nagar", 0.8), "address_line2": none(),
        "village_or_city": v("Rampur", 0.85), "district": v("Varanasi", 0.9), "state": v("Uttar Pradesh", 0.95),
        "pincode": v("221001", 0.9),
    }
    out.update(over)
    return out


def test_verhoeff():
    assert verhoeff_valid(VALID_AADHAAR)
    assert not verhoeff_valid("999941057059")
    assert not verhoeff_valid("abc")


def test_prepare_images(settings):
    blocks = prepare_images([(jpeg(exif_rotate=True), "image/jpeg")], settings)
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/jpeg"
    img = Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"])))
    assert max(img.size) == 1568 and img.size[1] > img.size[0]  # downscaled and EXIF-rotated upright
    pdf = prepare_images([(b"%PDF-1.4 fake", "application/pdf")], settings)
    assert pdf[0]["type"] == "document"
    with pytest.raises(IdImageError):
        prepare_images([(b"not an image", "image/jpeg")], settings)
    with pytest.raises(IdImageError):
        prepare_images([(jpeg((200, 120)), "image/jpeg")], settings)
    with pytest.raises(IdImageError):
        prepare_images([], settings)


async def test_extract_aadhaar(settings):
    llm = FakeLLM({"id_extract": aadhaar_out()})
    ex = await extract_id([(jpeg(), "image/jpeg"), (jpeg(), "image/jpeg")], llm=llm, settings=settings)
    content = llm.calls[0][1]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert ex.document_type is IdType.AADHAAR
    assert ex.fields["name"].value == "Priya Sharma"
    assert ex.fields["id_number"].value == VALID_AADHAAR and ex.fields["id_number"].confidence == 0.92
    assert ex.fields["dob"].value == "2004-08-15"
    assert ex.fields["address"].value["district"] == "Varanasi"
    assert ex.fields["address"].confidence == 0.8
    assert ex.extras["father_or_spouse_name"].value == "Rakesh Sharma"
    assert ex.low_confidence == ["address"]


def test_checksum_failure_and_masked_number():
    ex = postprocess(_IdOut.model_validate(aadhaar_out(id_number=v("9999 4105 7059", 0.95))), 0.85)
    assert ex.fields["id_number"].confidence == 0.3 and "id_number" in ex.low_confidence
    assert any("checksum" in i for i in ex.issues)

    ex = postprocess(_IdOut.model_validate(aadhaar_out(id_number=v("XXXX XXXX 7058", 0.99))), 0.85)
    assert "id_number" not in ex.fields and any("masked" in i for i in ex.issues)


def test_pan_format_and_year_only():
    out = aadhaar_out(document_type="pan", id_number=v("ABCDE12345", 0.95), dob=none(), year_of_birth=v("1990"))
    ex = postprocess(_IdOut.model_validate(out), 0.85)
    assert ex.fields["id_number"].confidence == 0.5
    assert "dob" not in ex.fields and any("year of birth" in i for i in ex.issues)


def test_not_an_id_and_bad_values():
    ex = postprocess(_IdOut.model_validate(aadhaar_out(is_identity_document=False)), 0.85)
    assert ex.fields == {} and ex.issues[0].startswith("This does not look")
    ex = postprocess(_IdOut.model_validate(aadhaar_out(pincode=v("2210"), dob=v("31/31/2004"), gender=v("blue"))), 0.85)
    assert "pincode" not in ex.fields["address"].value
    assert "dob" not in ex.fields and "gender" not in ex.fields
