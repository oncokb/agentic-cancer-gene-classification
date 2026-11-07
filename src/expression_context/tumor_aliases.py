"""Curated names for the 32 TCGA PanCancer cohorts (COAD+READ as COADREAD).

The literature gate's deterministic guard uses these, alongside ACGC's public
tumor-term matcher, to decide whether a paper mentions the requested tumor
type at all. Papers that fail the guard never reach the judge, so a missing
common name (ccRCC for KIRC, HCC for hepatocellular carcinoma, CRC for
COADREAD) would wrongly turn into "insufficient". The guard only admits
candidates; the LLM judge still decides whether a paper is tumor-specific, so
an ambiguous abbreviation costs tokens, not correctness.

- ``phrases`` match case-insensitively on word boundaries, with spaces and
  hyphens interchangeable ("clear-cell renal" == "clear cell renal").
- ``abbreviations`` match case-sensitively as whole words (so "PCa" and
  "ccRCC" don't match ordinary words). Two-letter abbreviations are left out
  as too ambiguous, and so is "BRCA" (the BRCA1/2 genes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class CohortNames:
    phrases: Tuple[str, ...]
    abbreviations: Tuple[str, ...]


def _c(phrases: Tuple[str, ...], abbreviations: Tuple[str, ...]) -> CohortNames:
    return CohortNames(phrases, abbreviations)


NSCLC_PHRASES = ("non-small cell lung cancer", "non-small cell lung carcinoma")

COHORT_NAMES: Dict[str, CohortNames] = {
    "ACC": _c(("adrenocortical carcinoma", "adrenocortical cancer", "adrenal cortical carcinoma",
               "adrenal cortical cancer"), ("ACC",)),
    "BLCA": _c(("bladder cancer", "bladder carcinoma", "urothelial carcinoma", "urothelial cancer",
                "urothelial bladder cancer", "bladder urothelial carcinoma"),
               ("BLCA", "UCB", "UBC", "MIBC", "NMIBC")),
    "BRCA": _c(("breast cancer", "breast carcinoma", "mammary carcinoma", "invasive ductal carcinoma",
                "invasive lobular carcinoma", "breast invasive carcinoma"),
               ("IDC", "ILC", "TNBC")),
    "CESC": _c(("cervical cancer", "cervical carcinoma", "cervical squamous cell carcinoma",
                "endocervical adenocarcinoma", "uterine cervix cancer"), ("CESC",)),
    "CHOL": _c(("cholangiocarcinoma", "biliary tract cancer", "bile duct cancer",
                "intrahepatic cholangiocarcinoma", "extrahepatic cholangiocarcinoma"),
               ("CHOL", "CCA", "iCCA", "eCCA")),
    "COADREAD": _c(("colorectal cancer", "colorectal carcinoma", "colorectal adenocarcinoma",
                    "colon cancer", "colon carcinoma", "colon adenocarcinoma", "rectal cancer",
                    "rectal carcinoma", "rectal adenocarcinoma"),
                   ("CRC", "mCRC", "COAD", "READ", "COADREAD")),
    "DLBC": _c(("diffuse large B-cell lymphoma", "diffuse large B cell lymphoma"),
               ("DLBCL", "DLBC")),
    "ESCA": _c(("esophageal cancer", "oesophageal cancer", "esophageal carcinoma",
                "oesophageal carcinoma", "esophageal adenocarcinoma", "oesophageal adenocarcinoma",
                "esophageal squamous cell carcinoma", "oesophageal squamous cell carcinoma"),
               ("ESCA", "ESCC", "EAC")),
    "GBM": _c(("glioblastoma", "glioblastoma multiforme"), ("GBM",)),
    "HNSC": _c(("head and neck squamous cell carcinoma", "head and neck cancer",
                "head and neck carcinoma", "oral squamous cell carcinoma", "oral cancer",
                "laryngeal cancer", "laryngeal squamous cell carcinoma", "oropharyngeal cancer"),
               ("HNSCC", "HNSC", "OSCC", "LSCC")),
    "KICH": _c(("chromophobe renal cell carcinoma", "chromophobe RCC", "renal chromophobe",
                "kidney chromophobe", "chromophobe kidney cancer"),
               ("chRCC", "ChRCC", "CHRCC", "KICH")),
    "KIRC": _c(("clear cell renal cell carcinoma", "clear cell renal carcinoma",
                "renal clear cell carcinoma", "clear cell RCC", "kidney renal clear cell carcinoma",
                "clear cell kidney cancer"),
               ("ccRCC", "CCRCC", "KIRC")),
    "KIRP": _c(("papillary renal cell carcinoma", "papillary RCC", "renal papillary carcinoma",
                "kidney renal papillary cell carcinoma"),
               ("pRCC", "PRCC", "KIRP")),
    "LAML": _c(("acute myeloid leukemia", "acute myeloid leukaemia", "acute myelogenous leukemia",
                "acute myelogenous leukaemia"), ("AML", "LAML")),
    "LGG": _c(("lower grade glioma", "low grade glioma", "diffuse glioma", "oligodendroglioma",
               "astrocytoma"), ("LGG", "LGGs")),
    "LIHC": _c(("hepatocellular carcinoma", "hepatocellular cancer", "liver cancer",
                "liver hepatocellular carcinoma", "hepatoma"), ("HCC", "LIHC")),
    "LUAD": _c(("lung adenocarcinoma",) + NSCLC_PHRASES, ("LUAD", "NSCLC")),
    "LUSC": _c(("lung squamous cell carcinoma", "squamous cell lung cancer",
                "squamous cell carcinoma of the lung") + NSCLC_PHRASES,
               ("LUSC", "SqCLC", "NSCLC")),
    "MESO": _c(("mesothelioma", "pleural mesothelioma"), ("MPM", "MESO")),
    "OV": _c(("ovarian cancer", "ovarian carcinoma", "high grade serous ovarian carcinoma",
              "high grade serous ovarian cancer", "serous ovarian cancer",
              "epithelial ovarian cancer"), ("HGSOC", "HGSC", "EOC")),
    "PAAD": _c(("pancreatic cancer", "pancreatic adenocarcinoma", "pancreatic carcinoma",
                "pancreatic ductal adenocarcinoma"), ("PDAC", "PAAD")),
    "PCPG": _c(("pheochromocytoma", "phaeochromocytoma", "paraganglioma"), ("PCPG", "PPGL")),
    "PRAD": _c(("prostate cancer", "prostate adenocarcinoma", "prostatic adenocarcinoma",
                "prostate carcinoma", "castration resistant prostate cancer"),
               ("PCa", "PRAD", "CRPC", "mCRPC")),
    "SARC": _c(("sarcoma", "soft tissue sarcoma", "leiomyosarcoma", "liposarcoma",
                "dedifferentiated liposarcoma", "undifferentiated pleomorphic sarcoma",
                "myxofibrosarcoma", "synovial sarcoma", "malignant peripheral nerve sheath tumor"),
               ("STS", "LMS", "DDLPS", "MPNST", "SARC")),
    "SKCM": _c(("melanoma", "cutaneous melanoma", "skin cutaneous melanoma"), ("SKCM",)),
    "STAD": _c(("gastric cancer", "gastric carcinoma", "gastric adenocarcinoma", "stomach cancer",
                "stomach adenocarcinoma"), ("STAD",)),
    "TGCT": _c(("testicular germ cell tumor", "testicular germ cell tumour", "testicular cancer",
                "seminoma", "nonseminoma", "non-seminomatous germ cell tumor"),
               ("TGCT", "TGCTs")),
    "THCA": _c(("thyroid cancer", "thyroid carcinoma", "papillary thyroid carcinoma",
                "papillary thyroid cancer", "differentiated thyroid cancer"),
               ("PTC", "THCA", "DTC")),
    "THYM": _c(("thymoma", "thymic carcinoma", "thymic epithelial tumor", "thymic epithelial tumour"),
               ("TET", "TETs", "THYM")),
    "UCEC": _c(("endometrial cancer", "endometrial carcinoma", "endometrial adenocarcinoma",
                "endometrioid carcinoma", "uterine corpus endometrial carcinoma"), ("UCEC",)),
    "UCS": _c(("uterine carcinosarcoma", "carcinosarcoma of the uterus",
               "malignant mixed Mullerian tumor", "malignant mixed müllerian tumor"),
              ("UCS", "MMMT")),
    "UVM": _c(("uveal melanoma", "ocular melanoma", "choroidal melanoma"), ("UVM",)),
}


def cohort_names(oncotree_code: str) -> CohortNames:
    return COHORT_NAMES.get((oncotree_code or "").strip().upper(), CohortNames((), ()))


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(word) for word in re.split(r"[\s-]+", phrase.strip()) if word]
    return re.compile(
        r"(?<![A-Za-z0-9])" + r"[\s-]+".join(words) + r"(?![A-Za-z0-9])", re.IGNORECASE
    )


def _abbreviation_pattern(abbreviation: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(abbreviation)}(?![A-Za-z0-9])")


@lru_cache(maxsize=64)
def _patterns(oncotree_code: str) -> Tuple[Tuple[str, re.Pattern[str]], ...]:
    names = cohort_names(oncotree_code)
    return tuple(
        [(phrase, _phrase_pattern(phrase)) for phrase in names.phrases]
        + [(abbr, _abbreviation_pattern(abbr)) for abbr in names.abbreviations]
    )


def cohort_terms_in_text(oncotree_code: str, text: str) -> List[str]:
    """Curated names of the cohort that occur in ``text``, in table order."""
    return [term for term, pattern in _patterns(oncotree_code) if pattern.search(text or "")]
