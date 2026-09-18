# Fixtures

`balance_sheet.json`, `profit_and_loss.json`, `trial_balance.json` and
`general_ledger.json` are Intuit's own published sample responses, extracted
from `refs/docs/CodesModelsJsonObjects_v2.json` — the JSON that backs
developer.intuit.com. They describe Intuit's demo company, not a real one, so
there is nothing here to redact.

Regenerate them from the vendored docs rather than editing by hand.

The malformed reports used in the invariant tests are built in the test module
itself. They are deliberately wrong, and keeping them next to the assertion
that catches them is clearer than a file that looks like real data.
