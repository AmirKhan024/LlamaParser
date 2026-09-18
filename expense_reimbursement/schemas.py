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


class LineItem(BaseModel):
    name: Optional[str] = None
    quantity: Optional[str] = None
    unit_price: Optional[Decimal] = None
    total: Optional[Decimal] = None


class BaseClaim(BaseModel):
    document_type: DocumentType
    vendor_name: Optional[str] = None
    date: Optional[str] = None  # kept as string at extraction time; parsed/validated later
    amount: Optional[Decimal] = None
    currency: Optional[str] = "INR"  # None when it couldn't be determined -- see validate.build_claim
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
    line_items: list[LineItem] = Field(default_factory=list)


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
    # Explicit schema fields (not just additional_fields) so the
    # extraction prompt asks the model for them by name every time --
    # additional_fields is free-form, model-chosen keys, which is why
    # the same document's subtotal/tax showed up under a different key
    # (or not at all) between runs. See validate._validate_generic_claim.
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    line_items: list[LineItem] = Field(default_factory=list)


# document_type -> schema class, used both to pick which schema to
# validate against (schema_for, below) AND to tell the model what
# fields exist for each type (extract.py's prompt lists every schema in
# this dict; a type left out only ever gets the base fields described).
# GENERIC_RECEIPT is listed explicitly -- not because it has its own
# subclass (it still uses GenericClaim), but because GenericClaim
# stopped being just the base fields once it gained `line_items`; left
# out of this dict, the model would never be told that field exists for
# a document classified generic_receipt. TAXI_RECEIPT, HOTEL_INVOICE,
# FUEL_RECEIPT and UNSTRUCTURED_PROOF still fall back to GenericClaim
# with no test data motivating anything more for them yet.
SCHEMA_BY_TYPE: dict[DocumentType, type[BaseClaim]] = {
    DocumentType.TELECOM_BILL: TelecomBill,
    DocumentType.LOCAL_CONVEYANCE_FORM: LocalConveyanceForm,
    DocumentType.RESTAURANT_BILL: RestaurantBill,
    DocumentType.APPROVAL_CORRESPONDENCE: ApprovalCorrespondence,
    DocumentType.GENERIC_RECEIPT: GenericClaim,
}


def schema_for(document_type: DocumentType) -> type[BaseClaim]:
    return SCHEMA_BY_TYPE.get(document_type, GenericClaim)
