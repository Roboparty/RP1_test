# External SDK and robot models

`rp1-test` does not vendor external motor SDKs or robot models. Paths are explicit.

## Choose a matching release

Use an **RP1 robot software release bundle that matches the deployed controller firmware**:

- `motors/` supplies the `motors_py` extension.
- `rp1_description/` supplies the robot model.

Before a real test, the operator must check out or unpack one approved immutable
release, record its real release tag or Git commit, and set:

```bash
export RP1_MOTORS_SDK_ROOT=/opt/rp1/releases/<approved-ref>/motors
export RP1_DESCRIPTION_ROOT=/opt/rp1/releases/<approved-ref>/rp1_description
```

Replace `<approved-ref>` with the release tag or commit selected for the tested robot. Follow each component's license and redistribution terms when distributing its source or binaries.

## Reproducible setup

1. Obtain both components from the same approved release.
2. Build/install `motors_py` using that release's build instructions.
3. Set the two environment variables above.
4. Run `./scripts/bootstrap.sh`; it validates that `motors_py` can be imported
   and that the model root contains a supported MJCF.
5. Record these facts in the run configuration/change record:
   - approved release tag or actual `git rev-parse HEAD`;
   - Python version and `pip freeze`;
   - SHA-256 of the selected MJCF;
   - motor firmware version and CAN adapter/backend.

The built-in adapter searches the active Python environment, then build/install
locations beneath `RP1_MOTORS_SDK_ROOT`. The model resolver searches only
`RP1_DESCRIPTION_ROOT` unless `--model` is provided. Neither path is inferred
from another checkout.
