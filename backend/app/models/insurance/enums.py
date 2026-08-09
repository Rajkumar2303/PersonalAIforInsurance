"""Enums for the canonical insurance intake schema (Issue #2).

Only used where they improve consistency; easy to extend with new members
without changing the models. Enum member names are uppercase; serialized
values are lowercase strings (e.g. ``InsuranceType.AUTO`` -> ``"auto"``).
"""

from __future__ import annotations

from enum import StrEnum


class InsuranceType(StrEnum):
    """Insurance product type. AUTO is the only fully implemented product."""

    AUTO = "auto"
    HOME = "home"
    TENANT = "tenant"
    LIFE = "life"
    TRAVEL = "travel"
    OTHER = "other"


class QuoteMode(StrEnum):
    """Purpose of the intake: an actual live quote or a discovery sweep."""

    LIVE_QUOTE = "live_quote"
    DISCOVERY = "discovery"


class ChannelType(StrEnum):
    """Communication channels the applicant permits for this journey."""

    EMAIL = "email"
    PHONE = "phone"
    SMS = "sms"
    BROKER = "broker"
    WEB_PORTAL = "web_portal"
    MAIL = "mail"
    IN_PERSON = "in_person"


class Province(StrEnum):
    """Canadian provinces and territories (primary address / licensing)."""

    AB = "AB"
    BC = "BC"
    MB = "MB"
    NB = "NB"
    NL = "NL"
    NS = "NS"
    NT = "NT"
    NU = "NU"
    ON = "ON"
    PE = "PE"
    QC = "QC"
    SK = "SK"
    YT = "YT"


class PreferredLanguage(StrEnum):
    ENGLISH = "english"
    FRENCH = "french"
    OTHER = "other"


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"
    NON_BINARY = "non_binary"
    OTHER = "other"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class MaritalStatus(StrEnum):
    SINGLE = "single"
    MARRIED = "married"
    COMMON_LAW = "common_law"
    SEPARATED = "separated"
    DIVORCED = "divorced"
    WIDOWED = "widowed"
    OTHER = "other"


class DriverRole(StrEnum):
    PRINCIPAL = "principal"
    SECONDARY = "secondary"
    OCCASIONAL = "occasional"


class LicenceStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    OTHER = "other"


class LicenceClass(StrEnum):
    """Ontario-focused licence classes; OTHER for out-of-province classes."""

    G1 = "G1"
    G2 = "G2"
    G = "G"
    M1 = "M1"
    M2 = "M2"
    M = "M"
    OTHER = "other"


class FuelType(StrEnum):
    GASOLINE = "gasoline"
    DIESEL = "diesel"
    HYBRID = "hybrid"
    PLUG_IN_HYBRID = "plug_in_hybrid"
    ELECTRIC = "electric"
    OTHER = "other"


class OwnershipType(StrEnum):
    OWNED = "owned"
    LEASED = "leased"


class PurchaseState(StrEnum):
    NEW = "new"
    USED = "used"


class VehicleUseType(StrEnum):
    PLEASURE = "pleasure"
    COMMUTE = "commute"
    SCHOOL = "school"
    BUSINESS = "business"
    FARM = "farm"
    COMMERCIAL = "commercial"


class CoverageSelectionState(StrEnum):
    """Explicit included / excluded / unknown for optional coverages."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"


class PaymentFrequency(StrEnum):
    ANNUAL = "annual"
    MONTHLY = "monthly"


class OwnDamageCoverageType(StrEnum):
    SPECIFIED_PERILS = "specified_perils"
    COMPREHENSIVE = "comprehensive"
    COLLISION = "collision"
    ALL_PERILS = "all_perils"


class OptionalBenefitType(StrEnum):
    """Accident-benefit optional items (Ontario OAF 1 relevant)."""

    INCOME_REPLACEMENT = "income_replacement"
    NON_EARNER = "non_earner"
    CAREGIVER = "caregiver"
    LOST_EDUCATIONAL_EXPENSES = "lost_educational_expenses"
    VISITOR_EXPENSES = "visitor_expenses"
    HOUSEKEEPING_HOME_MAINTENANCE = "housekeeping_home_maintenance"
    PERSONAL_ITEMS = "personal_items"
    DEATH = "death"
    FUNERAL = "funeral"
    DEPENDANT_CARE = "dependant_care"
    INDEXATION = "indexation"
    SUPPLEMENTARY_MEDICAL = "supplementary_medical"
    CATASTROPHIC_IMPAIRMENT = "catastrophic_impairment"


class EndorsementType(StrEnum):
    OPCF_20 = "OPCF 20"
    OPCF_27 = "OPCF 27"
    OPCF_43 = "OPCF 43"
    OPCF_44R = "OPCF 44R"


class DiscountType(StrEnum):
    BUNDLE = "bundle"
    MULTI_VEHICLE = "multi_vehicle"
    WINTER_TIRES = "winter_tires"
    THEFT_RECOVERY = "theft_recovery"
    DRIVER_TRAINING = "driver_training"
    CLAIMS_FREE = "claims_free"
    CONVICTION_FREE = "conviction_free"
    RETIREE = "retiree"
    AFFINITY = "affinity"
    TELEMATICS = "telematics"


class RelationshipType(StrEnum):
    SPOUSE = "spouse"
    COMMON_LAW = "common_law"
    PARENT = "parent"
    CHILD = "child"
    SIBLING = "sibling"
    ROOMMATE = "roommate"
    OTHER = "other"
