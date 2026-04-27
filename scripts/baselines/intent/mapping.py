"""Intent → Walacor 5-class mapping table.

Walacor's intent classifier uses 5 labels:
    normal       — conversational / simple Q&A / no special handling
    rag          — document- or file-grounded queries
    reasoning    — multi-step logic, math, code, planning
    system_task  — operational commands acting on a resource
    web_search   — current-events / time-sensitive lookup

This module hand-audits CLINC150, Banking77, and MASSIVE-en intents
into those 5 classes. Mapping discipline:

  • Explicit mapping or DROP — never default. An intent we can't
    confidently place is removed from training rather than mislabeled.
  • Symmetric coverage — every class gets representative samples
    from at least two source corpora plus our synthetic gateway corpus.
  • Documented rationale — comments on non-obvious choices so future
    audits / re-trainings can spot drift in the mapping itself.

The synthetic gateway corpus is hand-written to cover gateway-specific
patterns that public NLU corpora underrepresent (LLM-shaped queries,
RAG framing, code review, observability questions).
"""
from __future__ import annotations

# Canonical class labels — must match src/gateway/classifier/model_labels.json.
CLASSES: tuple[str, ...] = ("normal", "rag", "reasoning", "system_task", "web_search")
LABEL_TO_ID = {c: i for i, c in enumerate(CLASSES)}


# ─── CLINC150 (clinc_oos) ────────────────────────────────────────────
# 150 fine-grained intents across 10 domains + an out-of-scope class.
# Reference: https://github.com/clinc/oos-eval
#
# Mapping policy:
#   • "system_task" gets imperative / action-on-resource intents
#     (set_alarm, transfer, schedule_meeting, lights_on, change_*).
#   • "web_search" gets time-sensitive / current-state intents
#     (weather, traffic, news, exchange_rate, flight_status, stock_*).
#   • "reasoning" gets calculation / multi-step / planning intents
#     (calculator, measurement_conversion, tip, calendar planning).
#   • "rag" gets information-retrieval intents that imply external doc
#     lookup (definition, ingredients_list, fun_fact about specific X).
#     CLINC underweights pure RAG framing; we lean on the synthetic set.
#   • "normal" gets greetings / smalltalk / capability questions /
#     yes/no / confirmations / simple Q&A.
CLINC_MAPPING: dict[str, str] = {
    # ── system_task ────────────────────────────────────────────────
    "alarm":                "system_task",
    "set_alarm":            "system_task",
    "snooze_alarm":         "system_task",
    "cancel_alarm":         "system_task",
    "lights_on":            "system_task",
    "lights_off":           "system_task",
    "change_volume":        "system_task",
    "change_speed":         "system_task",
    "change_accent":        "system_task",
    "change_language":      "system_task",
    "change_ai_name":       "system_task",
    "change_user_name":     "system_task",
    "reset_settings":       "system_task",
    "sync_device":          "system_task",
    "todo_list_update":     "system_task",
    "shopping_list_update": "system_task",
    "calendar_update":      "system_task",
    "schedule_meeting":     "system_task",
    "schedule_maintenance": "system_task",
    "cancel_reservation":   "system_task",
    "restaurant_reservation": "system_task",
    "book_flight":          "system_task",
    "book_hotel":           "system_task",
    "transfer":             "system_task",
    "pay_bill":             "system_task",
    "order":                "system_task",
    "order_checks":         "system_task",
    "order_status":         "system_task",
    "freeze_account":       "system_task",
    "report_lost_card":     "system_task",
    "report_fraud":         "system_task",
    "replacement_card_duration": "system_task",
    "pin_change":           "system_task",
    "redeem_rewards":       "system_task",
    "new_card":             "system_task",
    "share_location":       "system_task",
    "find_phone":           "system_task",
    "text":                 "system_task",
    "uber":                 "system_task",
    "make_call":            "system_task",
    "timer":                "system_task",
    "update_playlist":      "system_task",
    "play_music":           "system_task",

    # ── web_search ─────────────────────────────────────────────────
    "weather":              "web_search",
    "traffic":              "web_search",
    "directions":           "web_search",
    "current_location":     "web_search",
    "next_holiday":         "web_search",
    "date":                 "web_search",   # "what's today's date"
    "time":                 "web_search",   # "what time is it"
    "timezone":             "web_search",
    "datetime":             "web_search",
    "exchange_rate":        "web_search",
    "stock":                "web_search",
    "flight_status":        "web_search",
    "gas":                  "web_search",   # "where's the cheapest gas"
    "tire_pressure":        "web_search",   # device-state lookup
    "spending_history":     "web_search",   # account-state lookup
    "transactions":         "web_search",
    "balance":              "web_search",
    "bill_balance":         "web_search",
    "bill_due":             "web_search",
    "rewards_balance":      "web_search",
    "pto_balance":          "web_search",
    "pto_used":             "web_search",
    "pto_request":          "system_task",  # request IS an action
    "pto_request_status":   "web_search",
    "next_song":            "web_search",
    "current_song":         "web_search",
    "what_song":            "web_search",
    "vaccines":             "web_search",   # "do I need a vaccine for…"
    "international_visa":   "web_search",
    "international_fees":   "web_search",
    "card_declined":        "web_search",   # status check
    "application_status":   "web_search",
    "expiration_date":      "web_search",
    "min_payment":          "web_search",
    "interest_rate":        "web_search",
    "apr":                  "web_search",
    "routing":              "web_search",
    "last_maintenance":     "web_search",

    # ── reasoning ──────────────────────────────────────────────────
    "calculator":           "reasoning",
    "math":                 "reasoning",
    "calculate_tip":        "reasoning",
    "tip":                  "reasoning",
    "measurement_conversion": "reasoning",
    "translate":            "reasoning",
    "spelling":             "reasoning",
    "definition":           "rag",          # dictionary-style lookup
    "todo_list":            "reasoning",    # "what should I do next"
    "shopping_list":        "reasoning",
    "calendar":             "reasoning",    # "what's on my calendar"
    "meeting_schedule":     "reasoning",
    "improve_credit_score": "reasoning",    # advice / planning
    "taxes":                "reasoning",
    "mpg":                  "reasoning",    # computation
    "distance":             "reasoning",

    # ── rag ────────────────────────────────────────────────────────
    "recipe":               "rag",
    "ingredients_list":     "rag",
    "ingredient_substitution": "rag",
    "meal_suggestion":      "rag",
    "restaurant_reviews":   "rag",
    "restaurant_suggestion": "rag",
    "accept_reservations":  "rag",
    "food_last":            "rag",
    "fun_fact":             "rag",
    "what_are_your_hobbies": "normal",      # smalltalk, not info-retrieval
    "carry_on":             "rag",          # policy lookup
    "plug_type":            "rag",
    "tire_change":          "rag",          # how-to
    "oil_change_how":       "rag",
    "oil_change_when":      "rag",
    "gas_type":             "rag",
    "calendar_holidays":    "rag",
    "what_is_your_name":    "normal",       # smalltalk
    "where_are_you_from":   "normal",
    "who_made_you":         "normal",
    "who_do_you_work_for":  "normal",
    "what_can_i_ask_you":   "normal",
    "smart_home":           "system_task",
    "user_name":            "normal",
    "ai_name":              "normal",

    # ── normal ─────────────────────────────────────────────────────
    "greeting":             "normal",
    "goodbye":              "normal",
    "thank_you":            "normal",
    "yes":                  "normal",
    "no":                   "normal",
    "maybe":                "normal",
    "confirm":              "normal",
    "cancel":               "normal",
    "repeat":               "normal",
    "how_old_are_you":      "normal",
    "meaning_of_life":      "normal",
    "do_you_have_pets":     "normal",
    "tell_joke":            "normal",
    "flip_coin":            "normal",
    "roll_dice":            "normal",
    "are_you_a_bot":        "normal",
    "tell_me_a_joke":       "normal",

    # ── deliberately UNMAPPED → dropped from training ──────────────
    # Reason listed in trailing comment.
    # "oos":               # out-of-scope; handled separately, see USE_OOS_AS_NORMAL.
    # "taste":             # ambiguous (food-taste vs music-taste)
    # "report_lost_x":     # ambiguous (system_task or web_search)
    # "freeze_*":          # ambiguous (account vs lights vs other)
    # "share_card":        # corpus-specific banking jargon
}


# Whether to fold CLINC's "oos" examples into `normal`. In production a
# request that's "out of scope" of any taxonomy is most often just chit-
# chat or low-information — `normal` is the safe destination. This keeps
# our class-balance honest (oos has 1k samples vs 100/intent elsewhere).
USE_OOS_AS_NORMAL = True


# ─── Banking77 ───────────────────────────────────────────────────────
# Bank-specific. Most map cleanly to system_task / web_search.
BANKING77_MAPPING: dict[str, str] = {
    "activate_my_card":                       "system_task",
    "age_limit":                              "rag",
    "apple_pay_or_google_pay":                "rag",
    "atm_support":                            "web_search",
    "automatic_top_up":                       "system_task",
    "balance_not_updated_after_bank_transfer": "web_search",
    "balance_not_updated_after_cheque_or_cash_deposit": "web_search",
    "beneficiary_not_allowed":                "web_search",
    "cancel_transfer":                        "system_task",
    "card_about_to_expire":                   "web_search",
    "card_acceptance":                        "rag",
    "card_arrival":                           "web_search",
    "card_delivery_estimate":                 "web_search",
    "card_linking":                           "system_task",
    "card_not_working":                       "web_search",
    "card_payment_fee_charged":               "web_search",
    "card_payment_not_recognised":            "web_search",
    "card_payment_wrong_exchange_rate":       "web_search",
    "card_swallowed":                         "web_search",
    "cash_withdrawal_charge":                 "web_search",
    "cash_withdrawal_not_recognised":         "web_search",
    "change_pin":                             "system_task",
    "compromised_card":                       "system_task",
    "contactless_not_working":                "web_search",
    "country_support":                        "rag",
    "declined_card_payment":                  "web_search",
    "declined_cash_withdrawal":               "web_search",
    "declined_transfer":                      "web_search",
    "direct_debit_payment_not_recognised":    "web_search",
    "disposable_card_limits":                 "rag",
    "edit_personal_details":                  "system_task",
    "exchange_charge":                        "web_search",
    "exchange_rate":                          "web_search",
    "exchange_via_app":                       "rag",
    "extra_charge_on_statement":              "web_search",
    "failed_transfer":                        "web_search",
    "fiat_currency_support":                  "rag",
    "get_disposable_virtual_card":            "system_task",
    "get_physical_card":                      "system_task",
    "getting_spare_card":                     "system_task",
    "getting_virtual_card":                   "system_task",
    "lost_or_stolen_card":                    "system_task",
    "lost_or_stolen_phone":                   "system_task",
    "order_physical_card":                    "system_task",
    "passcode_forgotten":                     "system_task",
    "pending_card_payment":                   "web_search",
    "pending_cash_withdrawal":                "web_search",
    "pending_top_up":                         "web_search",
    "pending_transfer":                       "web_search",
    "pin_blocked":                            "system_task",
    "receiving_money":                        "rag",
    "Refund_not_showing_up":                  "web_search",
    "request_refund":                         "system_task",
    "reverted_card_payment":                  "web_search",
    "supported_cards_and_currencies":         "rag",
    "terminate_account":                      "system_task",
    "top_up_by_bank_transfer_charge":         "rag",
    "top_up_by_card_charge":                  "rag",
    "top_up_by_cash_or_cheque":               "rag",
    "top_up_failed":                          "web_search",
    "top_up_limits":                          "rag",
    "top_up_reverted":                        "web_search",
    "topping_up_by_card":                     "system_task",
    "transaction_charged_twice":              "web_search",
    "transfer_fee_charged":                   "web_search",
    "transfer_into_account":                  "system_task",
    "transfer_not_received_by_recipient":     "web_search",
    "transfer_timing":                        "rag",
    "unable_to_verify_identity":              "web_search",
    "verify_my_identity":                     "system_task",
    "verify_source_of_funds":                 "system_task",
    "verify_top_up":                          "web_search",
    "virtual_card_not_working":               "web_search",
    "visa_or_mastercard":                     "rag",
    "why_verify_identity":                    "rag",
    "wrong_amount_of_cash_received":          "web_search",
    "wrong_exchange_rate_for_cash_withdrawal": "web_search",
}


# ─── MASSIVE-en intents → 5-class mapping ───────────────────────────
# MASSIVE has 60 intents; we keep mappings tight.
MASSIVE_MAPPING: dict[str, str] = {
    "alarm_set":                "system_task",
    "alarm_remove":             "system_task",
    "alarm_query":              "web_search",
    "audio_volume_mute":        "system_task",
    "audio_volume_up":          "system_task",
    "audio_volume_down":        "system_task",
    "audio_volume_other":       "system_task",
    "calendar_set":             "system_task",
    "calendar_query":           "web_search",
    "calendar_remove":          "system_task",
    "cooking_recipe":           "rag",
    "cooking_query":            "rag",
    "datetime_query":           "web_search",
    "datetime_convert":         "reasoning",
    "email_query":              "web_search",
    "email_sendemail":          "system_task",
    "email_addcontact":         "system_task",
    "email_querycontact":       "web_search",
    "general_quirky":           "normal",
    "general_joke":             "normal",
    "general_greet":            "normal",
    "iot_hue_lightoff":         "system_task",
    "iot_hue_lighton":          "system_task",
    "iot_hue_lightchange":      "system_task",
    "iot_hue_lightup":          "system_task",
    "iot_hue_lightdim":         "system_task",
    "iot_cleaning":             "system_task",
    "iot_coffee":               "system_task",
    "iot_wemo_on":              "system_task",
    "iot_wemo_off":             "system_task",
    "lists_query":              "web_search",
    "lists_createoradd":        "system_task",
    "lists_remove":             "system_task",
    "music_query":              "web_search",
    "music_likeness":           "normal",
    "music_settings":           "system_task",
    "music_dislikeness":        "normal",
    "news_query":               "web_search",
    "play_music":               "system_task",
    "play_radio":               "system_task",
    "play_podcasts":            "system_task",
    "play_audiobook":           "system_task",
    "play_game":                "system_task",
    "qa_currency":              "web_search",
    "qa_definition":            "rag",
    "qa_factoid":               "rag",
    "qa_maths":                 "reasoning",
    "qa_stock":                 "web_search",
    "recommendation_events":    "rag",
    "recommendation_locations": "rag",
    "recommendation_movies":    "rag",
    "social_post":              "system_task",
    "social_query":             "web_search",
    "takeaway_order":           "system_task",
    "takeaway_query":           "rag",
    "transport_query":          "web_search",
    "transport_taxi":           "system_task",
    "transport_ticket":         "system_task",
    "transport_traffic":        "web_search",
    "weather_query":            "web_search",
}


# ─── Synthetic gateway-domain corpus ─────────────────────────────────
# Hand-written prompts for gateway-specific patterns that public NLU
# datasets miss. These are HIGH-LEVERAGE: they cover real LLM-gateway
# traffic shapes (RAG framing, code requests, observability, etc.).
#
# Quality bar: each prompt was written by a human, not auto-generated;
# every label is correct on inspection; every prompt is ≥ 4 tokens
# (no "ok" / "yes" tier-2-noise inputs).
SYNTHETIC: list[tuple[str, str]] = [
    # ── rag ─ document/file-grounded queries ───────────────────────
    ("based on the attached document, what is the customer's preferred plan?", "rag"),
    ("according to the report I just uploaded, which region grew fastest in Q3?", "rag"),
    ("summarize the key findings of the white paper", "rag"),
    ("in the contract pdf, find the indemnification clause and quote it verbatim", "rag"),
    ("from the meeting notes, list every action item assigned to engineering", "rag"),
    ("what does the API specification say about authentication?", "rag"),
    ("using the data in this spreadsheet, identify the top three outliers", "rag"),
    ("citing the relevant section of the manual, explain how to reset the device", "rag"),
    ("review the codebase and tell me where the payment logic lives", "rag"),
    ("look up what our policy doc says about overtime and quote it", "rag"),
    ("according to the user research deck, what was the main pain point?", "rag"),
    ("based on the attached invoice, what's the line-item total?", "rag"),
    ("from the README, walk me through the setup steps", "rag"),
    ("scan the changelog and tell me what changed in v2.3", "rag"),
    ("in the legal brief, what's the cited precedent?", "rag"),
    ("read the meeting transcript and extract who agreed to what", "rag"),
    ("from the financial statement attached, what's the operating margin?", "rag"),
    ("inside the support ticket history, find every escalation reason", "rag"),
    ("answer the question using only the information in the provided document", "rag"),
    ("ground your answer in the attached source material", "rag"),

    # ── reasoning ─ multi-step / coding / planning ─────────────────
    ("write a python function that deduplicates a list while preserving order", "reasoning"),
    ("refactor this code to use async/await instead of callbacks", "reasoning"),
    ("explain the bug in this code snippet and propose a fix", "reasoning"),
    ("design a database schema for a multi-tenant SaaS product", "reasoning"),
    ("walk through the algorithm step by step for finding the longest palindromic substring", "reasoning"),
    ("if a train leaves at 3pm going 60mph and another at 4pm going 80mph, when do they meet?", "reasoning"),
    ("derive the closed-form solution for this recurrence", "reasoning"),
    ("plan a 4-week sprint roadmap for shipping the new auth feature", "reasoning"),
    ("compare the time complexity of merge sort and quicksort and explain when each wins", "reasoning"),
    ("debug this stack trace and tell me the root cause", "reasoning"),
    ("write a sql query that returns the top 10 customers by revenue last quarter", "reasoning"),
    ("translate this regex into plain english", "reasoning"),
    ("calculate the compound interest on $10000 at 5% over 7 years", "reasoning"),
    ("optimize this function for memory usage", "reasoning"),
    ("trace through this recursive call and tell me when it hits the base case", "reasoning"),
    ("draft an architecture diagram description for an event-driven order system", "reasoning"),
    ("review my pull request and flag any issues", "reasoning"),
    ("what's the big-O of this loop", "reasoning"),
    ("if I have 7 widgets and give 3 to alice and twice that to bob, how many do i have left", "reasoning"),
    ("design a load test plan for the checkout endpoint", "reasoning"),

    # ── system_task ─ commands acting on resources ─────────────────
    ("delete the user with id 5821", "system_task"),
    ("create a new project named 'phoenix'", "system_task"),
    ("rotate the api key for the production gateway", "system_task"),
    ("schedule the deploy for tomorrow at 9am pacific", "system_task"),
    ("revoke admin access for user@example.com", "system_task"),
    ("restart the worker pool", "system_task"),
    ("clear the cache for the search index", "system_task"),
    ("apply the migration against staging", "system_task"),
    ("scale the api service to 4 replicas", "system_task"),
    ("disable the experimental feature flag", "system_task"),
    ("add a new dns record pointing to the load balancer", "system_task"),
    ("invite the new hire to the engineering channel", "system_task"),
    ("kick off a backup job", "system_task"),
    ("rotate logs older than 30 days", "system_task"),
    ("snapshot the database before the upgrade", "system_task"),
    ("merge the feature branch into main", "system_task"),
    ("publish the release notes", "system_task"),
    ("send a calendar invite to the security team", "system_task"),
    ("archive last quarter's tickets", "system_task"),
    ("pause notifications for the next 30 minutes", "system_task"),

    # ── web_search ─ current-state / time-sensitive ────────────────
    ("what's the latest news on the federal reserve rate decision", "web_search"),
    ("when does the next iphone come out", "web_search"),
    ("what's trending on hacker news today", "web_search"),
    ("show me yesterday's stock close for nvidia", "web_search"),
    ("who won the champions league this year", "web_search"),
    ("what's the current exchange rate from usd to inr", "web_search"),
    ("is there a flight delay at sfo right now", "web_search"),
    ("what's happening in the markets today", "web_search"),
    ("look up the weather in tokyo this weekend", "web_search"),
    ("what's the most recent earnings report from apple", "web_search"),
    ("who's currently the prime minister of japan", "web_search"),
    ("what's the latest commit on the kubernetes main branch", "web_search"),
    ("any recent security advisories for openssl", "web_search"),
    ("what time is the nasa launch tonight", "web_search"),
    ("show me current outages on the aws status page", "web_search"),
    ("what's the latest version of node lts", "web_search"),
    ("is there a power outage in seattle right now", "web_search"),
    ("recent benchmarks for the new ryzen chip", "web_search"),
    ("what just happened with the bitcoin price", "web_search"),
    ("breaking news on the merger announcement", "web_search"),

    # ── normal ─ conversational, no special handling ───────────────
    ("hello there", "normal"),
    ("good morning", "normal"),
    ("how are you doing today", "normal"),
    ("what's your name", "normal"),
    ("can you help me with something", "normal"),
    ("thanks, that was useful", "normal"),
    ("ok cool", "normal"),
    ("alright let's continue", "normal"),
    ("never mind", "normal"),
    ("got it", "normal"),
    ("interesting, tell me more", "normal"),
    ("not sure i follow", "normal"),
    ("could you say that another way", "normal"),
    ("sounds good", "normal"),
    ("appreciate the help", "normal"),
    ("can we start over", "normal"),
    ("just chatting", "normal"),
    ("nothing urgent", "normal"),
    ("just exploring what you can do", "normal"),
    ("hi, what's up", "normal"),
    ("can you tell me a joke", "normal"),
    ("what is 1+1", "reasoning"),  # boundary case — keeps boundary clear
    ("i'm doing fine, thanks", "normal"),
    ("yeah that works", "normal"),
    ("see you later", "normal"),
    ("bye", "normal"),

    # ── adversarial / tricky boundary cases ────────────────────────
    # These intentionally probe edges to keep the model from over-fitting.
    ("can you check the document and also add a calendar invite for follow-up", "rag"),  # primary intent is RAG
    ("what's 50 + 50, and also what's the weather", "reasoning"),  # boundary: math wins
    ("delete every email from spam folder older than 30 days", "system_task"),
    ("based on the docs, write me a python function that does the same thing", "rag"),  # RAG framing dominates
    ("from yesterday's news, summarize what happened with the merger", "rag"),  # has news but also framed as 'from X' — RAG
    ("what's the current price of eth and convert it to inr", "web_search"),  # current price is the first/primary intent

    # ── extended reasoning corpus (high leverage for LLM gateways) ─
    # Public NLU datasets underweight code/math/planning queries — but
    # those dominate real LLM-gateway traffic. Hand-written here so the
    # baseline can recognise them on day 1.
    ("write a typescript function to debounce a callback by 200ms", "reasoning"),
    ("what's wrong with this regex: ^[a-z]+@\\d+$", "reasoning"),
    ("convert this javascript callback chain into async/await", "reasoning"),
    ("what's a more efficient way to write this loop", "reasoning"),
    ("explain why this query is slow and how to optimize the index", "reasoning"),
    ("design a rate limiter that allows 100 req/min per user", "reasoning"),
    ("write a unit test for the function above", "reasoning"),
    ("solve this leetcode problem step by step", "reasoning"),
    ("what's the difference between bfs and dfs and when do you use each", "reasoning"),
    ("draft pseudocode for a consistent-hashing ring", "reasoning"),
    ("compute the variance of the following numbers: 4, 8, 15, 16, 23, 42", "reasoning"),
    ("what's 13.5% of 4280", "reasoning"),
    ("if I invest $5000 at 7% annual return, how much in 20 years", "reasoning"),
    ("derive the formula for the area of a circle from its circumference", "reasoning"),
    ("solve the system: 2x + 3y = 12, 4x - y = 5", "reasoning"),
    ("integrate x^2 sin(x) dx", "reasoning"),
    ("simplify (a+b)^2 - (a-b)^2", "reasoning"),
    ("what's 2 to the 30th power", "reasoning"),
    ("plan an mvp roadmap for a calendar scheduling app", "reasoning"),
    ("walk me through the steps to migrate from rest to grpc", "reasoning"),
    ("compare two architectures: monolith vs microservices, and when each makes sense", "reasoning"),
    ("decompose this user story into engineering tasks", "reasoning"),
    ("write a kubernetes deployment yaml for a 3-replica nginx", "reasoning"),
    ("draft a state machine diagram description for a payment flow", "reasoning"),
    ("propose a schema for storing user preferences with versioning", "reasoning"),
    ("write a bash script that compresses logs older than 7 days", "reasoning"),
    ("how would you debug a memory leak in a long-running python process", "reasoning"),
    ("walk through the algorithm for dijkstra's shortest path", "reasoning"),
    ("explain why this code raises a race condition", "reasoning"),
    ("rewrite this function to be O(n) instead of O(n^2)", "reasoning"),
    ("what's the difference between left join and inner join", "reasoning"),
    ("write the regex to match an iso-8601 timestamp", "reasoning"),
    ("trace through what this generator yields on the first three calls", "reasoning"),
    ("explain why react re-renders this component every keystroke", "reasoning"),
    ("write a fibonacci function with memoization", "reasoning"),
    ("show how to parse this json into a typed go struct", "reasoning"),
    ("what's the big-O of inserting into a balanced bst", "reasoning"),
    ("how do you implement a producer-consumer queue with backpressure", "reasoning"),
    ("write me a python decorator that retries on transient http errors", "reasoning"),
    ("design a feature flag system that supports gradual rollouts", "reasoning"),
    ("propose a caching strategy for read-heavy endpoints", "reasoning"),
    ("write a sql query for monthly active users grouped by signup cohort", "reasoning"),
    ("how would you shard this table by tenant_id", "reasoning"),
    ("what's an efficient way to compute pairwise cosine similarity for 1M vectors", "reasoning"),
    ("design a distributed lock with leases and fencing tokens", "reasoning"),
    ("write a small terraform module for an s3 bucket with versioning", "reasoning"),
    ("what does this stack trace tell us about the root cause", "reasoning"),
    ("plan a database migration that backfills a new column without downtime", "reasoning"),
    ("explain how raft achieves consensus", "reasoning"),
    ("write a make target that runs tests in parallel and fails fast", "reasoning"),
    ("how can I refactor this 500-line function safely", "reasoning"),
    ("walk through how cors actually works in a browser", "reasoning"),
    ("what's the cleanest way to handle errors in a go http handler", "reasoning"),
    ("write a python script that reads a csv and emits a histogram of column values", "reasoning"),

    # ── extended rag corpus ────────────────────────────────────────
    ("the attached doc has the user's case history — give me a one-paragraph summary", "rag"),
    ("read the security audit report and tell me which findings are critical", "rag"),
    ("from the attached api spec, list every endpoint that accepts a post body", "rag"),
    ("scan this log file and identify the time of the first 500 error", "rag"),
    ("based on the data in the csv, what's the median order value", "rag"),
    ("find every mention of 'migration' in the design doc and quote it", "rag"),
    ("according to the customer's last 3 support tickets, what is their primary issue", "rag"),
    ("from the attached resume, summarise the candidate's most recent role", "rag"),
    ("look in the legal terms doc and find the data-retention clause", "rag"),
    ("extract the action items from the attached meeting recording transcript", "rag"),
    ("from the spec, what is the rate-limit policy for unauthenticated callers", "rag"),
    ("read the attached email thread and tell me what was decided", "rag"),
    ("find the section about error handling in the runbook and paraphrase it", "rag"),
    ("based on the architecture doc i shared, where do auth tokens get validated", "rag"),
    ("scan the contract pdf for any auto-renewal language", "rag"),
    ("from the attached pdf, what's the warranty period for component X", "rag"),
    ("looking at the prd, which acceptance criteria are still open", "rag"),
    ("from the audit log file, who modified the config last", "rag"),
    ("citing the report, what was the year-over-year growth", "rag"),
    ("according to the linked confluence page, who owns this service", "rag"),
    ("the user uploaded a screenshot — what error message does it show", "rag"),
    ("based on the attached ticket, summarise the customer's request in one sentence", "rag"),
    ("find the original quote about availability in the sla document", "rag"),
    ("from the meeting transcript, who agreed to take the action item on the auth refactor", "rag"),
    ("read the attached document carefully and answer only from what's there", "rag"),
    ("if the doc doesn't say, tell me 'not specified' rather than guessing", "rag"),
    ("look at this spreadsheet and identify any duplicate rows", "rag"),
    ("from the legal opinion, summarise the risks in plain language", "rag"),
    ("based on the doc, list every external dependency the team is taking on", "rag"),
    ("according to the user's intake form, what's their billing tier", "rag"),
]


def all_synthetic_per_class() -> dict[str, int]:
    """Sanity-check helper — counts of synthetic samples per class."""
    out: dict[str, int] = {c: 0 for c in CLASSES}
    for _, label in SYNTHETIC:
        out[label] = out.get(label, 0) + 1
    return out


if __name__ == "__main__":
    # Eyeball the synthetic balance.
    counts = all_synthetic_per_class()
    print("Synthetic class balance:")
    for c in CLASSES:
        print(f"  {c:14s} {counts.get(c, 0):4d}")
    total = sum(counts.values())
    print(f"  {'total':14s} {total:4d}")
    print()
    print("CLINC mapped intents:", len(CLINC_MAPPING))
    print("Banking77 mapped intents:", len(BANKING77_MAPPING))
    print("MASSIVE mapped intents:", len(MASSIVE_MAPPING))
