# Audit and reproducibility

Choose **Download audit ZIP** on Results, or **Download project and audit** in Project settings. The ZIP includes your current project and the persistent history of AI actions recorded by this version of MetaCoder.

## What is recorded

| Record | Included information |
| --- | --- |
| Application | App version, a copy of the application code, and details of the software environment used |
| Run | Selected PDFs, provider/model, pacing and parallelism, settings, final statuses, coded-data and evidence CSV snapshots |
| Inputs | Saved manual and coding sheet plus effective inputs passed to the action, original study PDFs or uploaded draft documents, conversion notes |
| Each request | Start and finish times, provider address, exact instructions and requested output format, selected model, and settings sent to the provider |
| Each response | Original response and any model version, request identifier, or usage details supplied by the provider |
| Outcomes | Validated result, any response-format repairs, errors, retries, and links between results and the actions that produced them |
| Export | Export timestamp, project identity, current run settings, and SHA-256 checksums for every other file in the ZIP |

All roles and prompts the app sends are in the request body. Native PDF request bodies include the encoded PDF; text-based requests include the extracted text exactly as sent. Server-side prompts or processing that a provider does not return cannot be captured.

AI manual drafting and coding-sheet conversion are recorded even if you discard the generated draft. Automatic retries are recorded separately; choosing to retry extraction also creates a new record. The working result files may be replaced, but the earlier audit records remain.

## Keep a record of your analysis

Wait for AI actions to finish, then download the audit ZIP and save it with your research materials. You do not need to run code or install additional software to keep this record. Export another copy after later changes or retries if you want to preserve those too.

For everyday checking, use the [Results page](results.md) to review coded values, supporting evidence, and the original AI responses. The ZIP also contains detailed records that a collaborator or technical reviewer can use to investigate how a result was produced.

The file named `export_manifest.json` lists the exported files and their checksums. A checksum is a value calculated from a file's contents; a reviewer can use it to check whether that file has changed. It is not a digital signature or independent proof of when the work was performed.

An unfinished action may appear in an export taken while it is still running. That record does not establish that the provider completed the request. Export after actions finish for a complete record.

## Retention and credentials

Changing a manual or clearing generated output removes the working results but preserves `audit/`, including historical copies of inputs and outputs. Removing a source PDF also leaves any previously recorded input snapshot in the audit history. Deleting the whole project removes its audit history too. Keep exported ZIPs if you need an independent backup.

API authentication headers and keys are excluded. If a known API key is echoed in a response, it is replaced with `[REDACTED]` in the audit record. Exports still contain your research documents, prompts, conversion notes, endpoint addresses, and model output.

## Limits of reproducibility

Older runs cannot acquire missing timestamps, exact prompts, or full response envelopes retrospectively. Their surviving result files are still exported; absent audit records mean provenance is incomplete.

The selected model ID is always recorded. A provider-reported model/version or fingerprint is recorded only when supplied, otherwise it is null. Each PDF's result file and YAML record also note the model the provider reported serving, which can differ from the selected model when a provider redirects an older model ID to a newer one. A reported model name may still be an alias rather than an immutable model revision. Unspecified generation parameters use provider defaults, which are not inferred or invented in the audit log.

You can inspect and reconstruct the recorded requests, but hosted model changes, hidden server settings, PDF preprocessing, and nondeterminism can prevent byte-identical results. There is no automatic replay feature. Repeating a request requires provider access and may incur a new charge.
