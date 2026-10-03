# Extraction implementation notes

This document describes the code that runs today. Measurements refer to the
synthetic, privacy-safe images in `tests/fixtures/documents/`; they are regression
evidence, not a claim of production accuracy on unseen paperwork.

## Provider and model path

The implemented provider is OpenAI:

```dotenv
EXTRACTION_PROVIDER=openai
OPENAI_EXTRACTION_MODEL=gpt-6-astra
```

`OpenAIVisionProvider` sends the prepared image to the OpenAI Responses API with
`store=false` and uses `responses.parse(..., text_format=<Pydantic schema>)`.
The same configured model performs classification and typed extraction. Tests
inject `FakeVisionProvider`, so the suite does not make network calls.

The code has a provider protocol and settings for Anthropic, Google, fallback
models, repeated runs, and selected second-opinion fields. Those are scaffolding,
not active behavior: `get_provider()` currently accepts only `openai`, and the
processing path makes one classification call and one extraction call per
accepted image. Do not budget, deploy, or write product copy as though automatic
fallback or cross-provider voting exists.

Model availability and behavior must be confirmed with the deployment's OpenAI
account. The repository does not contain a live-provider acceptance test or a
measured cost claim.

## Processing sequence

1. Validate the upload by decoded image type, byte size, pixel count, and minimum
   dimensions. JPEG, PNG, WebP, HEIC, and HEIF are accepted.
2. Normalize EXIF orientation and resize the long edge to at most 1600 pixels.
   Classification sees this neutral version before any document-specific color
   transform.
3. Ask the model for the primary document type, number of physical documents,
   additional rotation, visibility of the entire page, and visible evidence for
   the classification.
4. Reject unknown documents, Florida Lottery draw schedules, more than one
   physical document in frame, and conflicts with an explicitly requested
   capture slot. A cropped daily-report or payout document receives a hard
   review check. A cropped delivery-invoice section receives a warning because
   long distributor receipts are intentionally photographed top-to-bottom with
   one overlapping product row.
5. Re-prepare the image using the detected document's transform and ask for the
   one matching strict schema.
6. Run deterministic evidence and arithmetic checks. A hard failure routes the
   document and submission to human review; it does not repair the model output.
7. Store the result, provider/model name, preprocessing steps, and check results
   beside the immutable original image.

Provider calls occur outside database transactions. Document claiming and
result persistence use short transactions so a slow model request does not hold
a row lock. A completed document is idempotent unless an explicit retry forces a
new read.

## Evidence, not model confidence

Every extracted field uses an `EvidenceValue` containing:

- the typed value or null;
- exact visible text;
- whether the field is printed;
- whether it is legible; and
- a short visual location.

The schema forbids extra fields and deliberately has no `confidence` property.
A model's confidence in its output is not proof that a digit is visible. The
application instead checks observable evidence and independently recomputes
printed identities where the document permits it.

Examples include:

- net sales from gross sales, returns, and discounts;
- Square report total from net sales, tax, tips, and gift-card sales;
- cash plus card versus total collected;
- drawer expected cash from opening cash, cash sales/refunds, and paid in/out;
- the Square report cash value versus drawer cash sales;
- invoice cases multiplied by reviewed Square units per case, plus loose
  bottles/cans received; and
- invoice line totals and grand total when the required printed operands exist.

An absent value is not zero. A visible but unreadable value is not guessed. A
printed zero is accepted only with visible text supporting it.

## Document-specific preprocessing

There is no universal enhancement pipeline. A transform that helps one fixture
can damage another.

### Pink Florida Lottery stock

The watermark is magenta while the printed text is near-black. Taking the red
channel suppresses the background more effectively than grayscale on the two
current lottery fixtures.

Measured watermark intrusion, where lower is better:

| Fixture | Grayscale | Red channel |
| --- | ---: | ---: |
| Daily scratch-off sales | 22.3% | 1.4% |
| Ticket balance detail | 22.2% | 1.2% |

The preprocessing tests recompute the relevant contrast behavior so an image
library change cannot silently replace the transform.

### Square drawer screen

The fixture has a diagonal illumination streak but no meaningful pure-white
clipping. The implementation uses flat-field normalization followed by CLAHE;
it does not inpaint, because inpainting would invent background where the glare
crosses real characters.

| Measurement | Value |
| --- | ---: |
| Pixels clipped to pure white | 0.0000% |
| Local contrast, raw | 1.02 |
| Local contrast, transformed | 1.93 |

### Square thermal report and other images

The white Square receipt is already high contrast. It receives only orientation
normalization and resizing. Other/unknown documents use the neutral path.

### Distributor delivery invoices

Private representative examples were reviewed for Johnson-style tabular pages
and Southern-style long scan sheets. The originals are not repository fixtures:
they contain merchant account details, addresses, and signatures. Regression
tests use synthetic structured rows instead.

The extractor maps `CASES/BTL` or `CS/BT`, `QPC/BPC`, item size, distributor
product number, UPC, net unit cost, and extended line total explicitly. A long
receipt is assembled from ordered crops by removing only an exact suffix/prefix
row overlap. Exact duplicate source photos are ignored. Strong duplicate rows
which cannot be proven to be a crop overlap block posting for owner review.

Zero-quantity backorders are retained as evidence but excluded from Square.
Nested consumer packs such as a case containing two 12-packs remain ambiguous
until an owner-reviewed Square mapping establishes whether the store sells
packs or singles.

## Why classification rejects extra documents

Because two documents can contain plausible money values, letting the model
choose which numbers "belong" would be unsafe. The classifier therefore counts
physical documents before extraction and requires exactly one.

The draw-games schedule is another deliberate negative case. It prints on the
same stock as financial lottery reports but contains no reconciliation money;
the processing code classifies and rejects it instead of producing plausible
zeros.

## Known gaps before production use

- The current drawer fixture is open/current, not an ended drawer with counted
  cash and over/short.
- The lottery fixtures contain zero activity and incomplete ticket ranges.
- There is no publishable real distributor invoice fixture and no
  payout-evidence fixture. Private examples informed synthetic invoice tests.
- There is no corpus-level accuracy result for the configured OpenAI model.
- Automatic fallback, independent second opinions, and another provider are not
  implemented.

Production validation therefore needs representative real documents with
independently transcribed ground truth. See
[Product status and validation](product-notes.md).
