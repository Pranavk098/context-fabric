# v2 registered runs, recomputed with once-per-message.id usage (EXPLORATORY)

Runs in records: 132. Runs with missing transcripts: 0.

Ratio = geometric mean over tasks of arm/reference mean cost, priced as Opus 5.5, with the registered hierarchical bootstrap 95% CI.

## Arm vs off (registered off-based ratios are VOID)

| stratum | arm | registered | dedup, final session | dedup, all sessions |
| --- | --- | --- | --- | --- |
| short | shipped | 0.877 (0.713-1.099) | 1.002 (0.960-1.038) | 1.002 (0.960-1.038) |
| short | checkpoint | 0.842 (0.666-1.056) | 0.999 (0.962-1.043) | 0.999 (0.962-1.043) |
| medium | shipped | 0.685 (0.601-0.794) | 0.761 (0.712-0.832) | 0.761 (0.712-0.832) |
| medium | checkpoint | 0.570 (0.486-0.667) | 0.500 (0.433-0.557) | 1.046 (0.984-1.106) |
| long | shipped | 0.545 (0.488-0.612) | 0.624 (0.555-0.684) | 0.624 (0.555-0.684) |
| long | checkpoint | 0.240 (0.196-0.322) | 0.299 (0.245-0.411) | 0.781 (0.714-0.848) |

## Checkpoint vs shipped (reported, not claimed)

| stratum | registered | dedup, final session | dedup, all sessions |
| --- | --- | --- | --- |
| short | 0.960 (0.761-1.127) | 0.997 (0.952-1.050) | 0.997 (0.952-1.050) |
| medium | 0.832 (0.700-1.002) | 0.656 (0.536-0.750) | 1.374 (1.286-1.452) |
| long | 0.441 (0.351-0.568) | 0.479 (0.381-0.628) | 1.251 (1.182-1.309) |

## deferred_tools_delta attachments per measured run (all sessions of the run)

| stratum | arm | per-run counts |
| --- | --- | --- |
| long | checkpoint | [3, 8, 3, 2, 2, 2, 3, 3, 3] |
| long | shipped | [1, 1, 1, 1, 1, 1, 1, 1, 3] |
| long | off | [27, 24, 24, 14, 19, 16, 20, 20, 20] |
| medium | checkpoint | [2, 3, 2, 2, 2, 2, 2, 2, 2, 3, 2, 2] |
| medium | shipped | [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1] |
| medium | off | [7, 7, 6, 8, 8, 8, 8, 8, 8, 9, 6, 8] |
| short | checkpoint | [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1] |
| short | shipped | [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1] |
| short | off | [2, 2, 2, 2, 3, 3, 2, 3, 1, 2, 3, 3] |
