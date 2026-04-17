"""Phase 25 Task 25: offline sanity fixtures.

Each `*_sanity.json` file contains labeled examples the
`SanityRunner` uses to gate a candidate BEFORE it enters shadow
validation. Format:

    {
      "model_name": "intent",
      "examples": [
        {"input": "search for ...", "label": "web_search"},
        ...
      ]
    }

These seed files ship small counts so the framework is exercised in
CI. Real deployments should grow them to the plan's 50-per-class
targets before relying on the accuracy gate.
"""
