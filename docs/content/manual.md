# Coding manual

The coding manual defines the fields MetaCoder extracts for each effect. You can edit fields directly, draft them from a document, or import an existing YAML manual.

[![Coding manual methods and the coding fields editor](assets/screenshots/manual.png)](assets/screenshots/manual.png)

## Edit coding fields

Open **Coding manual** and choose **Manual editing**. Give each field a clear name, type, and description. For categorical fields, define the allowed levels and explain when each applies. Remove the starter example if it does not fit your analysis.

Descriptions should be relative to your [effect definition](analysis.md). For instance, a publication-status field could allow “Published” and “Unpublished,” while a numerical field could record the number of participants contributing to that effect.

Every extraction request sends the model the analysis description, the effect definition, each field’s name, type and description, and each level with its description. Write them as instructions another coder could follow. Level descriptions matter most when level values are short codes such as `1` and `2`. The model must choose exactly one level per field; if several seem to apply, it picks the best fit and explains in the `notes` field.

Choose **Validate and save** when the fields and effect definition are ready. Fix any validation messages before continuing.

## Draft from a document

Choose **AI draft from document** and provide a PDF, DOCX, RTF, or Markdown manual. Drafting uses the global [drafting provider and model](settings.md), independently of extraction settings.

The generated fields appear in the editor as an unsaved draft. Review the effect definition, field descriptions, types, and categories, then choose **Validate and save draft**. A generated draft does not replace your saved manual until you save it.

## Import a manual

Choose **Import manual.yml** to import an existing manual. A minimal example looks like this:

```yaml
name: interference_analysis
effect_definition: Difference between incongruent and congruent response times.
effects:
  Sample size:
    type: number
    description: Number of participants contributing to this effect.
  Publication status:
    type: string
    levels:
      - value: Published
        description: Published in a peer-reviewed venue.
      - value: Unpublished
        description: Thesis, preprint, or other unpublished report.
```

## Changing a saved manual

Saving a changed manual clears earlier extraction results. Export any results you want to keep before changing definitions. A reset restores the starter manual, whose effect definition must be completed again.
