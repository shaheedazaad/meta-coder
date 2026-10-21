# Validating the coding

An LLM can code quickly and consistently, but it can also misread a table, pick the wrong condition, or apply a category loosely. Before you rely on its output, compare it with human coding on a sample of studies, and report how you did so.

Evidence synthesis guidance expects this level of care. The [Cochrane Handbook](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05) (section 5.5) recommends that at least two people extract data independently, and a [2025 joint position statement](https://doi.org/10.1186/s13750-025-00374-5) by Cochrane, the Campbell Collaboration, JBI, and the Collaboration for Environmental Evidence asks that AI be used with human oversight and reported transparently.

## Choose a validation sample

- **Sample studies, not rows.** Rows from the same PDF share a request and tend to share errors. Draw a random sample of PDFs and check every row in each.
- **Decide the size in advance.** There is no fixed rule. Choose a sample large enough that each important category appears several times and that agreement statistics are not driven by a handful of studies. Rare categories need more studies, or a deliberate extra sample of studies likely to contain them.
- **Include the difficult cases.** Make sure the sample covers long articles, multi-study papers, and any study types you expect to be hard. Add these on top of the random sample rather than replacing it, so your agreement figures stay representative.
- **Record how you drew the sample**, for example the random seed or list used.

## Code the sample independently

1. Give human coders the same coding manual and coding sheet rows as the model.
2. Ask them to code without seeing MetaCoder's output, so that their judgements are independent.
3. Record their codes in a spreadsheet with the same `row_id` and field names as `coded_data.csv`.
4. Treat "not reported" as a value in its own right: a missing value coded as present is an error.

Use a version of the manual that will not change during validation. If you revise the manual afterwards, see [Act on disagreements](#act-on-disagreements).

## Compare the codes

Export `coded_data.csv` from [Results](results.md) and join it with the human codes on `row_id`. Report agreement separately for each field, because a high overall figure can hide one unreliable field.

| Field type | Suggested measures |
| --- | --- |
| Categorical (nominal) | Percentage agreement plus Cohen's kappa for two coders, or Krippendorff's alpha, which also handles more than two coders and missing values |
| Ordinal | Weighted kappa or Krippendorff's alpha for ordinal data |
| Numeric (sample sizes, means, statistics) | Proportion of exact matches, the size of any differences, and an intraclass correlation (ICC) for agreement where values vary across studies |

Kappa can be low when one category is very common, even if raw agreement is high, so report percentage agreement alongside it. For numeric inputs to effect sizes, exact agreement matters more than correlation: one transposed digit can change a study's weight. Use an ICC form that measures absolute agreement rather than consistency.

There is no universal threshold. Decide in your protocol what level of agreement you will accept for each field, and what you will do if a field falls short.

## Act on disagreements

For each disagreement, return to the PDF and decide which value is correct. Use MetaCoder's evidence to locate the source quickly:

- `evidence.csv` and the per-PDF YAML give the page and quotation for each coded value.
- The raw response on **Results** shows the model's full answer, including notes on uncertain rows.
- The [audit ZIP](auditing.md) records the exact request, model, and settings that produced each value.

Then classify the cause:

- **Model error.** The value does not match the article. Note whether errors cluster in one field or study type.
- **Human error.** The model was right. Count these too; they show the value of independent checking.
- **Manual ambiguity.** Both readings are defensible. Clarify the field description or categories in the coding manual.

If you change the manual, saving it clears working results and the next run codes all studies again. Check the new run on a fresh random sample, not only on the studies used to refine the manual, because agreement on those studies will overstate accuracy.

For fields with low agreement, consider having a human check every study, or coding that field by hand.

## Report your methods

Describe LLM-assisted coding in enough detail for others to understand and repeat it. Include:

- The provider, the model ID, and any model version reported in the audit records.
- The dates of the extraction runs and key settings, such as the Gemini service tier or reasoning effort.
- The coding manual used for the final run, as a supplement, and how it was developed.
- That MetaCoder sent one request per PDF with the full PDF (or extracted text), and the MetaCoder version from the audit record.
- The validation sample: how it was drawn, its size, and how human coders were blinded.
- Agreement statistics for each field, before any disagreements were resolved.
- How disagreements were resolved, and which fields or studies were checked by humans in full.
- Where the audit records are available. Share prompts and responses as supplementary material if possible, but do not publish copyrighted PDFs: the audit ZIP contains the source documents.

## Further reading

- Cohen, J. (1960). A coefficient of agreement for nominal scales. *Educational and Psychological Measurement, 20*(1), 37–46. <https://doi.org/10.1177/001316446002000104>
- Feinstein, A. R., & Cicchetti, D. V. (1990). High agreement but low kappa: I. The problems of two paradoxes. *Journal of Clinical Epidemiology, 43*(6), 543–549. <https://doi.org/10.1016/0895-4356(90)90158-L>
- Hayes, A. F., & Krippendorff, K. (2007). Answering the call for a standard reliability measure for coding data. *Communication Methods and Measures, 1*(1), 77–89. <https://doi.org/10.1080/19312450709336664>
- Koo, T. K., & Li, M. Y. (2016). A guideline of selecting and reporting intraclass correlation coefficients for reliability research. *Journal of Chiropractic Medicine, 15*(2), 155–163. <https://doi.org/10.1016/j.jcm.2016.02.012>
- Gartlehner, G., Kahwati, L., Hilscher, R., et al. (2024). Data extraction for evidence synthesis using a large language model: A proof-of-concept study. *Research Synthesis Methods, 15*(4), 576–589. <https://doi.org/10.1002/jrsm.1710>
