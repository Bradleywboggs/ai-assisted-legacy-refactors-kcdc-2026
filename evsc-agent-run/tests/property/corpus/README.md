# Counterexample corpus

Scenarios the property suite found and could not explain. Each is committed with
its seed and the failures it produced, so it can be replayed verbatim:

```bash
make prop-replay FILE=corpus/<file>.json
```

**Currently empty.** Five counterexamples have lived here and all five were
retired after being resolved:

| Root cause | Resolution |
|---|---|
| `la: "banana"` throwing and abandoning the rest of its claimed batch | Real defect. Documented as [known issue 14](../../../docs/known-issues.md#14-one-malformed-frame-strands-the-rest-of-its-batch), pinned by characterization case 25, and modelled by property P14 |
| A non-integer `as` segment violating `meter_events.wh INT` during provisioned multi-connector fan-out | Real defect. Documented as [known issue 15](../../../docs/known-issues.md#15-an-empty-as-field-strands-multi-connector-frames), pinned by characterization case 26. The oracle initially failed to model it and was corrected in `lib/oracle.py::_fanout_throws` |

Retiring a file is correct once the behavior is pinned by a characterization
case: the case is a permanent gate that runs every time, whereas a corpus entry
only runs when someone replays it. See
[the promotion workflow](../README.md#promoting-a-counterexample).
