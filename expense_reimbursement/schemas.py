"""Flat Pydantic schemas for expense-claim extraction.

Deliberately simple: one small class per document type, all fields
except `document_type` optional, no dynamic field-name generation and
no shared ontology across types. Anything the model finds that doesn't
map to a named field goes in `additional_fields` rather than being
dropped or forcing a new field onto every schema.

Add a new DocumentType / subclass only when a real test document needs
one -- do not pre-build for types with no test data.
"""

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    TELECOM_BILL = "telecom_bill"
    RESTAURANT_BILL = "restaurant_bill"
    TAXI_RECEIPT = "taxi_receipt"
    LOCAL_CONVEYANCE_FORM = "local_conveyance_form"
    HOTEL_INVOICE = "hotel_invoice"
    FUEL_RECEIPT = "fuel_receipt"
    GENERIC_RECEIPT = "generic_receipt"
    UNSTRUCTURED_PROOF = "unstructured_proof"
    APPROVAL_CORRESPONDENCE = "approval_correspondence"


class BaseClaim(BaseModel):
    document_type: DocumentType
    vendor_name: Optional[str] = None
    date: Optional[str] = None  # kept as string at extraction time; parsed/validated later
    amount: Optional[Decimal] = None
    currency: str = "INR"
    additional_fields: dict[str, str] = Field(default_factory=dict)
    extraction_notes: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class TelecomBill(BaseClaim):
    account_number: Optional[str] = None
    billing_period: Optional[str] = None
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    total: Optional[Decimal] = None


class LocalConveyanceForm(BaseClaim):
    employee_name: Optional[str] = None
    employee_no: Optional[str] = None
    travel_entries: list[dict] = Field(default_factory=list)  # date, place, purpose, client, kms
    total_kms: Optional[Decimal] = None
    total_conveyance_amount: Optional[Decimal] = None
    daily_allowance_amount: Optional[Decimal] = None
    vehicle_maintenance_amount: Optional[Decimal] = None
    mobile_allowance_amount: Optional[Decimal] = None
    total_claimed: Optional[Decimal] = None


class RestaurantBill(BaseClaim):
    restaurant_gstin: Optional[str] = None
    subtotal: Optional[Decimal] = None
    cgst: Optional[Decimal] = None
    sgst: Optional[Decimal] = None
    grand_total: Optional[Decimal] = None


class ApprovalCorrespondence(BaseClaim):
    """An email/message thread *about* a claim (e.g. forwarding a form
    for sign-off), not the claim/receipt itself -- kept separate from
    LocalConveyanceForm etc. rather than forcing an email into a form
    schema it only partially resembles."""

    sender: Optional[str] = None
    recipient: Optional[str] = None
    sent_date: Optional[str] = None
    subject: Optional[str] = None
    approval_status: Optional[str] = None  # e.g. "approved", "pending" -- inferred from body text
    related_form_title: Optional[str] = None


class GenericClaim(BaseClaim):
    pass  # fallback for anything that doesn't match a known type well


# document_type -> schema class. Only the types with an actual subclass
# above are listed here; anything else (TAXI_RECEIPT, HOTEL_INVOICE,
# FUEL_RECEIPT, GENERIC_RECEIPT, UNSTRUCTURED_PROOF) falls back to
# GenericClaim, since there's no test data yet motivating a dedicated
# schema for them.
SCHEMA_BY_TYPE: dict[DocumentType, type[BaseClaim]] = {
    DocumentType.TELECOM_BILL: TelecomBill,
    DocumentType.LOCAL_CONVEYANCE_FORM: LocalConveyanceForm,
    DocumentType.RESTAURANT_BILL: RestaurantBill,
    DocumentType.APPROVAL_CORRESPONDENCE: ApprovalCorrespondence,
}


def schema_for(document_type: DocumentType) -> type[BaseClaim]:
    return SCHEMA_BY_TYPE.get(document_type, GenericClaim)
