# crm-document-ingestion

Turns business documents (DOCX, PDF, spreadsheets, scans, ...) into CRM-ready JSON:
organisations, contacts and document references, every value carrying a confidence and
the exact block/page/text it was read from. Documents come from local disk or from
OneDrive / SharePoint through Microsoft Graph.

Nothing in the core is client-specific: the entity types ship as generic starter schemas,
and a client's own fields are added as JSON schema files, no code needed.

## Architecture

```
 local file ─────────────────────────┐
                                      ▼
 OneDrive / SharePoint ──► connector ──► DownloadedFile ──► IngestionPipeline ──► IngestedDocument
 (sharing URL or           (Graph API,   (bytes + Source-    (document-extractor     (Document + source
  drive_id + item_id)       auth, limits)  Metadata)          .extract(bytes, ...))   metadata, sha256)
                                                                                         │
                                                                                         ▼
 CRM JSON  ◄── EntityValidator ◄── CompositeExtractor ◄── EntityExtractors ◄── CRMExtractionService
 (entities,    (schema types,       (merge per entity      (Intelligence,       (SchemaRegistry:
  provenance,   required fields,     type, keep all         Pattern, your own)    generic + client
  validation)   min confidence)      evidence)                                    JSON schemas)
```

`CRMIngestionApp` wires the three layers together; the `crm-ingest` CLI is a thin shell
over it.

### Package layout

```
src/crm_ingestion/
  app.py                  CRMIngestionApp: downloader (lazy) -> pipeline -> CRM service
  cli.py, __main__.py     crm-ingest / python -m crm_ingestion
  config.py               Settings from env vars and .env (pydantic-settings)
  errors.py               IngestionError hierarchy
  connectors/sharepoint/  Graph client, client-credentials auth, downloader, models
  ingestion/              IngestionPipeline, DocumentExtractorAdapter, local files
  crm/
    schemas/              EntitySchema/FieldDefinition, SchemaRegistry, generic schemas,
                          CRMEntity/FieldValue/Evidence/ExtractionResult
    extractors/           EntityExtractor contract, PatternExtractor, IntelligenceExtractor,
                          CompositeExtractor
    validators/           EntityValidator, ValidationReport
    service.py            CRMExtractionService, CRMProcessingResult
tests/                    connectors/, ingestion/, crm/ unit tests + test_end_to_end.py
```

### How document-extractor is used

The extraction library (`document_extractor`, repo `text_extraction_and_ocr`) is a separate
project. This package consumes **only its public API** and never modifies it:

```python
DocumentExtractor(options).extract(content_bytes, filename="contract.docx")  # -> Document
```

`DocumentExtractorAdapter` (in `ingestion/pipeline.py`) builds the extractor lazily:

- If `DOCUMENT_EXTRACTOR_CONFIG` is set, `DocumentExtractor()` is created with no options
  and the library reads that TOML file itself. `CRM_EXTRACTION_MODE` / `--mode` are then
  ignored.
- Otherwise it uses `ExtractionOptions(mode=CRM_EXTRACTION_MODE)`: `fast` (text layer only,
  no OCR), `balanced` (default, OCR for scans and text-bearing pictures) or `accurate`.
  `balanced` and `accurate` need the library's `ocr` extra and the Tesseract binary.

> **WARNING: document text can leave the machine.** A `DOCUMENT_EXTRACTOR_CONFIG` file with
> `[intelligence] enabled = true` makes document-extractor send document text to the LLM
> endpoint configured in that file (`base_url`). `IntelligenceExtractor` then maps the
> model's output to CRM entities. Only enable it with an endpoint you are allowed to send
> these documents to. `crm-ingest config check` flags it.

## Install

Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\activate
# document-extractor is proprietary and not on PyPI: install it from its repo or a wheel
pip install -e D:\text_extraction_and_ocr            # or: pip install path\to\document_extractor-*.whl
pip install -e ".[msal,dev]"                         # msal: SharePoint auth; dev: tests and linters
```

For OCR (the `balanced`/`accurate` modes on scans) also install document-extractor's `ocr`
extra and Tesseract with the language packs you need (see that repo's README).

## Configuration

Settings come from environment variables, or a `.env` file in the **current working
directory** (copy `.env.example`). All are optional for local files.

| Variable | Default | Meaning |
|---|---|---|
| `MICROSOFT_TENANT_ID` | empty | Entra ID tenant (directory) id |
| `MICROSOFT_CLIENT_ID` | empty | App registration (client) id |
| `MICROSOFT_CLIENT_SECRET` | empty | App client secret (never logged or printed) |
| `MICROSOFT_GRAPH_BASE_URL` | `https://graph.microsoft.com/v1.0` | Graph endpoint; change only for sovereign clouds |
| `MICROSOFT_AUTHORITY_HOST` | `https://login.microsoftonline.com` | Entra ID authority host |
| `MICROSOFT_GRAPH_SCOPE` | `https://graph.microsoft.com/.default` | Token scope |
| `MICROSOFT_GRAPH_TIMEOUT_SECONDS` | `30` | HTTP timeout for Graph calls |
| `MICROSOFT_MAX_DOWNLOAD_BYTES` | `104857600` (100 MB) | Largest file downloaded |
| `DOCUMENT_EXTRACTOR_CONFIG` | unset | TOML settings file read by document-extractor itself; wins over `CRM_EXTRACTION_MODE` |
| `CRM_EXTRACTION_MODE` | `balanced` | `fast` / `balanced` / `accurate` |
| `CRM_MIN_CONFIDENCE` | `0.5` | Entities/fields below this get a validation warning |
| `CRM_SCHEMA_PATHS` | `[]` | JSON list of client schema files, e.g. `["schemas/client.json"]`; relative paths resolve against the working directory |

`crm-ingest config check` shows what is set (secrets only as `set`/`missing`).

## Plugging in Microsoft credentials

Local files need nothing. For OneDrive/SharePoint the app uses the OAuth client-credentials
flow (app-only, no user sign-in):

1. In the Entra admin center, **App registrations > New registration** (single tenant).
2. **Certificates & secrets > New client secret**; copy the value.
3. **API permissions > Add > Microsoft Graph > Application permissions**: `Files.Read.All`
   and `Sites.Read.All`, then **Grant admin consent**. (For fewer rights, `Sites.Selected`
   plus a per-site grant also works for SharePoint document libraries.)
4. Set the three variables (in `.env` or the environment):
   ```
   MICROSOFT_TENANT_ID=<directory id>
   MICROSOFT_CLIENT_ID=<application id>
   MICROSOFT_CLIENT_SECRET=<secret value>
   ```
5. `pip install -e ".[msal]"` if not done, then run `crm-ingest config check`. It should say
   `SharePoint is configured` (credentials are verified on the first real call).

Until then any SharePoint call fails fast, **before any network request**, with
`ConfigurationError: Microsoft Graph credentials are not configured; set MICROSOFT_TENANT_ID, ...`.

## CLI

```
crm-ingest file PATH [-o FILE] [--mode fast|balanced|accurate] [--crm-only]
crm-ingest sharepoint (--url URL | --drive-id ID --item-id ID) [-o FILE] [--mode ...] [--crm-only]
crm-ingest config check
crm-ingest schemas list [--json]
python -m crm_ingestion ...         # same thing
```

- Output is JSON on stdout, or UTF-8 in `--output FILE`. By default the full
  `CRMProcessingResult` (`extraction` with source metadata, entities with per-field
  evidence and alternatives, warnings; `validation` report). `--crm-only` prints just the
  flat CRM entity list.
- Exit codes: `0` success (also when validation reports issues — check
  `validation.valid`), `1` `config check` found a broken setting (e.g. a schema file that
  does not load), `2` an error: any `IngestionError` (one line on stderr), missing input
  file, invalid settings, bad arguments.

```console
$ crm-ingest file profile.docx --mode fast --crm-only
[
  {
    "name": "Northwind Traders Ltd",
    "_meta": {
      "entity_type": "organization",
      "confidence": 0.85,
      "fields": {"name": {"confidence": 0.85, "extractor": "pattern",
                          "provenance": [{"block_id": "...:b1", "page": 1,
                                          "source_text": "Company: Northwind Traders Ltd", ...}]}},
      ...
  },
  {"full_name": "Maria Anders", "job_title": "Sales Director",
   "email": "sales@northwind.example.com", "phone": "+442079460958", "_meta": {...}}
]

$ crm-ingest sharepoint --url "https://contoso.sharepoint.com/:w:/s/Sales/Eabc..."
crm-ingest: error: ConfigurationError: Microsoft Graph credentials are not configured; set MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET
```

## Python usage

```python
from crm_ingestion import CRMIngestionApp

with CRMIngestionApp() as app:                     # settings from env / .env
    result = app.process_file("profile.docx")
    # result = app.process_sharing_url("https://contoso.sharepoint.com/:w:/s/Sales/Eabc...")
    # result = app.process_drive_item(drive_id, item_id)

print(result.valid)                                # validation outcome
for entity in result.to_crm_json():                # flat CRM JSON, with "_meta" provenance
    print(entity["_meta"]["entity_type"], {k: v for k, v in entity.items() if k != "_meta"})
print(result.extraction.source["filename"])        # where it came from
json_text = result.to_json(indent=2)               # the full result
```

The layers can be used on their own:

```python
from crm_ingestion import (
    CRMExtractionService, DocumentDownloader, DriveItemReference, IngestionPipeline,
    Settings, downloaded_file_from_path,
)
from crm_ingestion.config import ExtractionSettings

settings = Settings(extraction=ExtractionSettings(mode="fast"))
pipeline = IngestionPipeline(settings=settings)            # real document-extractor
service = CRMExtractionService(settings=settings.crm)

ingested = pipeline.ingest(downloaded_file_from_path("profile.docx"))
print(ingested.document.media_type, ingested.content_sha256[:12], ingested.warnings)
result = service.process(ingested)

# SharePoint (needs credentials):
downloader = DocumentDownloader.from_settings(settings.microsoft)
reference = DriveItemReference.from_sharing_url("https://contoso.sharepoint.com/:w:/s/Sales/Eabc...")
# result = service.process(pipeline.ingest_from(downloader, reference))
```

`CRMIngestionApp(settings=..., pipeline=..., service=..., downloader=..., downloader_factory=...)`
accepts any component pre-built; the SharePoint downloader is only created when a drive
item is processed, so local use never needs credentials.

## Adding a client schema

A schema is data. Field `type` is one of `string`, `number`, `integer`, `boolean`, `date`,
`email`, `phone`, `url`, `list`; values are validated and normalised by type (ISO dates,
digit-only phones, numbers from `EGP 1.234,50`, ...). `aliases` are the labels the field
may appear under in documents (any language), matched case- and punctuation-insensitively
in `Label: value` lines and two-column tables. `pattern` is a full-match regex.

`schemas/service_contract.json` (an illustrative example, not a real client's fields):

```json
{
  "schemas": [
    {
      "name": "service_contract",
      "description": "A signed service agreement.",
      "aliases": ["contract", "agreement"],
      "display_field": "contract_number",
      "fields": [
        {"name": "contract_number", "type": "string", "required": true,
         "aliases": ["Contract No", "Agreement Number", "رقم العقد"], "pattern": "[A-Z]{2,4}-\\d{3,8}"},
        {"name": "start_date", "type": "date", "aliases": ["Start Date", "Effective Date"]},
        {"name": "monthly_fee", "type": "number", "aliases": ["Monthly Fee", "Fee"]},
        {"name": "account_manager_email", "type": "email", "aliases": ["Account Manager"]}
      ]
    }
  ]
}
```

A file may hold one schema object, a list of them, or `{"schemas": [...]}`. Register it:

- **By configuration:** `CRM_SCHEMA_PATHS=["schemas/service_contract.json"]`. The files are
  loaded in order on top of the generic `organization`, `contact` and `document_reference`
  schemas (a name that is already registered is an error). Check with
  `crm-ingest schemas list`.
- **In code:**
  ```python
  from crm_ingestion import CRMExtractionService, default_registry

  registry = default_registry()
  registry.load_json("schemas/service_contract.json")        # or registry.register_from_dict({...})
  service = CRMExtractionService(registry=registry)
  ```

A schema can also be a Pydantic model: `registry.register_model(MyModel, name="...")`
(per-field `json_schema_extra={"type": "email", "aliases": [...]}`).

## Writing a custom EntityExtractor

An extractor takes a document-extractor `Document` and the registry and returns
`CRMEntity` objects. It must not modify the document; raising is allowed
(`CompositeExtractor` turns it into a warning and keeps the other extractors' output).
Extractors listed first are merged first; per field the highest-confidence value wins and
the rest are kept as `alternatives`, with all evidence.

```python
from document_extractor import Document

from crm_ingestion import CRMEntity, CRMExtractionService, EntityExtractor, SchemaRegistry
from crm_ingestion.crm import Evidence, FieldValue, PatternExtractor


class HeadingOrganizationExtractor(EntityExtractor):
    """Takes the first heading as the organisation name."""

    name = "heading-org"

    def extract(self, document: Document, *, schemas: SchemaRegistry) -> list[CRMEntity]:
        if "organization" not in schemas:
            return []
        for block in sorted(document.blocks, key=lambda b: b.reading_index):
            if block.kind == "heading" and block.text.strip():
                page = block.provenance[0].page if block.provenance else None
                evidence = Evidence(block_id=block.id, page=page, source_text=block.text, extractor=self.name)
                value = FieldValue(value=block.text.strip(), confidence=0.6, evidence=[evidence], extractor=self.name)
                return [
                    CRMEntity(
                        entity_type="organization",
                        fields={"name": value},
                        source_document_id=document.id,
                        extractors=[self.name],
                    )
                ]
        return []


service = CRMExtractionService([HeadingOrganizationExtractor(), PatternExtractor()])
```

The defaults are `IntelligenceExtractor` (maps `Document.intelligence` when the library's
intelligence ran; otherwise returns nothing) followed by `PatternExtractor` (regex and
label/value rules, driven entirely by the registered schemas).

## Testing

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m mypy src tests       # strict, with the pydantic plugin
.venv\Scripts\python.exe -m ruff check src tests
```

No test touches the network or Microsoft: Graph is faked with `httpx.MockTransport`
(`tests/connectors/fakes.py`), and `tests/conftest.py` removes every `MICROSOFT_*`,
`CRM_*` and `DOCUMENT_EXTRACTOR_CONFIG` variable and runs each test in a temporary
directory, so a developer's `.env` is never read. `tests/test_end_to_end.py` runs the
whole chain on a real DOCX (generated with python-docx) with the real document-extractor
in `fast` mode, plus the façade and the CLI.

## Docker

document-extractor is not on PyPI, so the image installs it from a local `wheels/`
directory (git-ignored):

```powershell
copy D:\text_extraction_and_ocr\dist\document_extractor-0.1.0.dev0-py3-none-any.whl wheels\
# or build a current one:  python -m pip wheel --no-deps --wheel-dir wheels D:\text_extraction_and_ocr
docker build -t crm-ingest .
docker run --rm --env-file .env -v "${PWD}\input:/data:ro" crm-ingest file /data/profile.docx --crm-only
docker compose run --rm crm-ingest config check
```

The image (python:3.12-slim) includes Tesseract with English and Arabic data for the
`balanced`/`accurate` modes, runs as a non-root user, and has `ENTRYPOINT ["crm-ingest"]`.

## Errors

Everything this package raises on purpose derives from `IngestionError`:

```
IngestionError
├── ConfigurationError        missing/invalid setting (message names the env vars), msal missing
├── AuthenticationError       Entra ID token could not be obtained (secrets scrubbed)
├── GraphAPIError             Graph error response (.status_code, .code)
│   ├── GraphTransportError   Graph unreachable (status_code 0)
│   └── ItemNotFoundError     404: no such item, or the app cannot see it
├── DownloadError             content download failed, too large, or not a file
├── DocumentExtractionFailed  document-extractor failed (original error is __cause__)
└── EntityExtractionError     a CRM entity extractor failed (fail_fast mode)
```

`IngestionPipeline.ingest_many` reports per-file `IngestionError`s and continues; the CLI
prints them as one line on stderr with exit code 2.
