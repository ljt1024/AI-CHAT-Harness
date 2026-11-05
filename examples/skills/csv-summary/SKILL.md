# CSV Summary

Use this skill to inspect CSV data with Python's standard library.

Run `python <skill-directory>/scripts/summarize.py <csv-path>` using run_command.
Replace `<skill-directory>` with this skill's Directory from the enabled skills context.
All paths must be inside /workspace. Request approval before running the command.

When no CSV is supplied, use `<skill-directory>/data/example.csv` as a sample.
Report the actual row count, column names and numeric column totals from the script output.
Do not invent results. This skill requires no dependencies or network access.
