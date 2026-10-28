# Test fixtures

Small offline excerpts used by `tests/`. Tests never access the network.

| Source | Origin |
|---|---|
| fr_bdpm | Real BDPM lines (7 CIS, 29/09/2026 edition), original encodings preserved (CIS/COMPO cp1252, CIP UTF-8). Page HTML reduced to the relevant links. |
| us_nadac | `sample.json`: real DKAN datastore response (12 rows, as of 09/30/2026). `nadac.csv`: real header + those rows + 5 synthetic rows (ATORVASTATIN/METFORMIN/AMOXICILLIN) to test the "latest As of Date" filter. `search.json`: real DKAN search response (NADAC datasets only). |
| us_asp | Real "section 508" CSV (first 45 lines, October 2026) re-zipped; crosswalk ZIP is a placeholder. |
| it_aifa | Real Class A / Class H CSV lines (31/05/2026), cp1252, `;`. |
| es_nomenclator | Real CSV lines (October 2026 export). |
| uk_drug_tariff | Real Part VIIIA CSV lines (October 2026). |
| uk_dmd | **Synthetic** XML following the dm+d data model (no TRUD key available while building); VMPP ids aligned with the Drug Tariff excerpt. |
| jp_nhi | Real oral/dental Excel rows; injection/topical files are reduced copies with one realistic row each. |
| br_cmed | Real PMC/PMVG workbooks reduced to 15 rows; PMVG formulas kept (`=ROUND(Nx*(1-0.1979),2)`) without cached values. |
| de_festbetrag | First 3 pages of the real BfArM PDF (Stand 15.09.2026). |
| de_amnog | HTML reproducing the real table structure with real rows (2 pages). |
| de_gba | Complete official 2012 Linagliptin and 2015 Lurasidon narrative-only justifications (excluded), 2020 Niraparib, 2022 Ivacaftor, 2025 Dupilumab, 2025 Artesunat (grouped integer clinic purchase/VAT/total), German/English 2025 Lisocabtagen maraleucel, and 2026 Nipocalimab PDFs. Reduced two-page catalogue/HTML fixtures test complete pagination, original preference, both English filename variants, multiline ingredients, wrapped currency, pack prices, rebates, clinic costs, page continuations and annual-cost exclusion. |
| es_cima | Reduced official Dupixent search/medication JSON with national presentation code 727309; pagination and invalid responses are generated in tests. |
| es_cipm | Complete official CIPM 273, 272, 250, 245 (Dupixent), 235 (previous/proposed columns), 187 (legacy prices/ingredients), 177 unpriced note and October 2026 informational note; reduced archive HTML includes a duplicate link. Tests distinguish proposed industrial prices, merged-cell geometry, meeting versus effective dates, unsupported rows, denied decisions and unpriced PDFs. |
| ca_hc_dpd | Real DPD extract lines for 8 drug codes. |
| ca_on_odb | **Synthetic** legacy XML and reduced publication-page HTML. Modern nested generic groups, ministry payments, non-benefit flags and sibling isolation are covered inline in the Canadian tests; adapter also verified against the live September 2026 extract. |
| ca_bc_pharmacare | Reduced October 2026 PDDF rows including Dupixent, with synthetic duplicates, cross-plan and unpriced records for aggregation and placeholder tests; zipped in memory by the offline HTTP harness. |
| cn_nhsa | Example manual-import CSV (template format). |
| cn_vbp | Complete public NHSA GY-YD2024-2 proposed-results PDF (385 price rows), plus two 2025 supplier-only PDFs without prices, explicitly rejected. Historical/proposed dates and billing units are not inferred. |
