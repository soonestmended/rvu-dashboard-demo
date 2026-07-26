Fixture data for `test_ingest_pipeline.py`.

- `mini_powerscribe_anon.csv` — PowerScribe-shaped rows (int CPTs, long status codes).
- `mini_mpower_anon.csv` — mPower-shaped rows for the SAME exams (float CPTs, short codes).

Both CSVs must match the column shape `ingest_csv_files` expects. The pair is designed to
exercise cross-source dedup: after ingesting both, exam counts should reflect the union
minus duplicates.
