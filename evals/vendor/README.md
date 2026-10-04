# Pinned upstream scoring

`martian.py` contains verbatim functions/constants from
withmartian/code-review-benchmark at commit
`e616e849755441da38f18bf3adba2c9583b03803` (MIT, see LICENSE):

- `offline/code_review_benchmark/step3_judge_comments.py`: judge prompt,
  batching, sibling propagation and review evaluation.
- `offline/code_review_benchmark/step2_5_dedup_candidates.py`: strict dedup
  prompt and group parser.
- `offline/analysis/score_profiles.py`: categories and aggregate scoring.

Only imports, the `LLMJudge` type alias, and batch-size constant are supplied
locally. Transport, CLI, and dashboard code are omitted. Do not silently fix
upstream scoring semantics here: a change requires a new scorer version.

The upstream per-review raw precision and profile aggregate precision use
different denominators. Our report uses `score_tools` for **both** per-case and
aggregate profile metrics. A candidate can match multiple goldens in this
upstream version; this is not the local legacy one-to-one scorer.

Our adapter uses structured agent issues directly (description + suggestion),
instead of LLM extraction from GitHub comments, and an OpenAI judge endpoint.
These are local evaluations using upstream scoring, not official leaderboard
measurements. Judge failures are excluded from completed scores and shown as
incomplete; they are never converted into valid false negatives.
