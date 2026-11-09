# AWS Qualification

Biblios is qualified on its own on one EC2 instance with the shelves on an EBS volume. The environment is disposable: it is stood up for a run, the qualification lanes run on the instance, the evidence is collected off it, and it is torn down with absence proved. Every step that creates, changes, spends or reads in the account stands behind one typed confirmation that names the account, the region, the resources, the calls, the data sent and the cost; nothing is deployed, destroyed or invoked by configuration alone.

## What a run proves

Three lanes run on the instance against shelves on the data volume, and the lane records say what each established:

- The hermetic lane runs the product proof suite with its temporary shelves on the volume (`--basetemp` under the mount) and the three demos with the deterministic embedder; no model and no network.
- The ingestion lane adds the operator's own documents of several kinds through Drawbridge as Biblios integrates it and reports, per kind, how many converted and how many were truthfully marked unconverted, together with which conversion tools were present on the instance. It passes only when `file`, Tesseract and Vellric were present and the corpus held at least one PDF, office, image and text document; otherwise it reports `incomplete` with the missing prerequisites named, which is not a qualification.
- The portability lane seals a shelf, copies the directory to a second directory on the same volume, opens the copy read-only, verifies it, searches it, then unseals the copy, reconciles it at its new root and proves the index was rebuilt there and still answers; the original stays sealed.
- The Bedrock lane builds and searches one shelf through Palace's `bedrock` provider. It runs only when named (`--lane bedrock`), only when the settings declare `bedrock` with exactly the model and region the confirmation named, only when those settings carry Palace's complete published-corpus assertion (the flag, a note saying what is sent and where it already lives, and the time of the assertion; Palace refuses any remote embedding without it, and the lane refuses first so the record says why), and only with a confirmed text count that covers its fixed input of one short document and one query; otherwise it reports `not-run` with the reason. The committed `qualification/bedrock-settings.yaml` carries that assertion for the lane's synthetic fixture and nothing else; a local proof reads it through Biblios and Palace. The lane's search runs Palace's own reranker from the model the bundle carries, and the lane passes only when that reranker was applied (fused order is a fallback, not a qualification). Before the instance is destroyed the lane exports Palace's per-request audit records (kind, status, model, dimension, timing; no text, no credential) as the result artifact `bedrock-audit.jsonl`, and both the lane and the runner's judge require at least one successful retention check and one embedding request in it: a Bedrock lane that passed on the instance without that artifact is a failed qualification, not a quiet success.

Before any lane runs, every shelf root is asserted to sit on the data volume and not the root filesystem; a run that waived that assertion is marked so in its record and is not an EBS qualification. `run` exits 0 only when Session Manager reported the command's success and every requested lane passed; any other outcome is exit 3 with the reasons, and the lane records, log and exit status are retained in the evidence directory whatever happened.

## The environment

Two stack sets live in `infra/`, synthesized by one CDK app and described by capability, never by account or resource identifier:

- The private qualification shape: one VPC with one private subnet and no internet or NAT gateway, Session Manager reached over interface endpoints, an S3 gateway endpoint for the staged bundle, a security group that admits nothing in, one private TLS-only bucket, one run-specific permissions boundary written by CDK from the bucket's real name (this run's inputs and results, Session Manager, the account reads, the one model when enabled; streaming, KMS, IAM and role assumption denied), one instance role under that boundary with Session Manager core, the run's bucket prefixes and, only when the Bedrock lane is enabled, the one embedding model, and one current-generation instance with IMDSv2 required, no public address, a root volume and one gp3 data volume mounted as the shelves' filesystem. The data volume's encryption property is neither set nor cleared: the account's default decides, and Biblios makes no encryption claim either way.
- The low-security example: a public subnet behind an internet gateway, a security group with no inbound rules, Session Manager over the internet, and the run's existing bucket and instance profile. It is deployed and torn down separately, after the private shape, to demonstrate and document the simpler shape; nothing qualifies on it.

Every resource carries the run's tag, and teardown is proved by reading the tag-scoped inventory back after the stacks are destroyed and checking each listed resource natively; a stale index entry is named as such, a live resource fails the teardown.

## Running it

Record the hourly figures you read from the price pages, with their date, in `qualification/cost-assumptions.json`; the packets print them. Then, from the repository root:

```sh
./bin/qualify-aws --dry-run plan
```

`plan` synthesizes offline, under a sanitized environment and a socket guard, and prints what each stack would create. `--dry-run` on any other command renders every confirmation packet in order, with placeholders where the account would have answered, and performs nothing.

Build the bundle on a machine with Docker and prove it offline before the first deploy. The bundle is one archive, `bundle.tar`, built from the repository at HEAD (the working tree's uncommitted state does not travel) beside a manifest of every file it holds; the proof extracts that same archive in a fresh container with the network disabled, installs the product environment from the staged cache with the same `uv sync` line `./bin/setup` runs for the product, and runs the hermetic and portability lanes through `qualification/lanes.py`, exactly the route the instance takes:

```sh
./bin/python qualification/bundle.py build --bundle ~/biblios-bundle --arch arm64 --corpus ~/Documents/corpus --tools ~/biblios-tools --log ~/biblios-bundle.log
```

```sh
./bin/python qualification/bundle.py prove --bundle ~/biblios-bundle --arch arm64 --log ~/biblios-bundle-proof.log
```

Every command but `plan` takes the profile, the region and the run id before the verb. The whole qualification is one invocation, `qualify`: it deploys the five stacks in order (network, bucket, the run-specific permissions boundary written by CDK from the bucket's real name, the role under it, the instance), verifies the caller, stages and runs the lanes, collects everything the instance left under the run's results prefix, and tears down in reverse, every step still behind its own typed confirmation. A failure after any stack exists enters the scoped teardown of exactly the recorded stacks (each destroy confirmed) rather than leaving billing resources up; `--keep` leaves the environment up and says so. Setup and teardown are not separate chores:

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> qualify --bundle ~/biblios-bundle
```

A fixture owned by someone else runs the same way without touching Biblios's pins: `qualify --fixture <archive> --fixture-command <the owner's shell file>` stages the owner's archive under the run's inputs prefix, extracts it on the data volume, runs exactly the owner's script (shown verbatim in the packet) with `RESULTS` set, and collects everything under the results prefix; a nonzero script exit is exit 3.

The separate verbs remain for recovery and for inspection. `deploy --stacks <contiguous list>` continues from the recorded state (a stack already created is refused; custody needs the boundary stack or `--boundary-arn`), and deploy, run, render and tear down can be confirmed one verb at a time:

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> deploy --boundary-arn <the account's workload boundary>
```

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> run --bundle ~/biblios-bundle
```

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> results
```

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> teardown
```

The first packet of a deploy asks before the read-only preflight (caller identity, the region's S3 prefix list, the current Amazon Linux 2023 image), which records the run's target; each stack then has its own packet, and the stacks created so far are recorded one by one, so a declined or failed later stack leaves an exact record for `teardown`. `run` and `teardown` begin with a disclosed read-only identity check and refuse when the caller, region or profile differs from the recorded target. `run` asks before staging the archive and its manifest, before the Bedrock calls when that lane is named (model, region, text count and the operator's cost figure for it, a stated bound and not an enforced meter), and before the Session Manager command, whose exact text is in the packet; `--keep` leaves the environment up and records that choice. `teardown` asks before listing the bucket and retrieving any result artifact not yet retained locally, then asks once for the removal of exactly the run's two prefixes (every listing page, deletions in batches, the count and scope shown), verifies they are empty, destroys the created stacks in reverse with one packet each (the boundary after the role that used it), and reads the tag-scoped inventory back, verifying every listed resource against its own service, because the tag index lags deletion: terminated, deleted or not-found resources are reported as stale, and any resource that is verifiably still live makes the teardown exit 3 with its list rather than claiming completion. Every object under the run's results prefix is retained locally before the first deletion, so nothing unique is lost to cleanup. Objects outside the run's prefixes are never touched; while any remain, the bucket is retained and reported rather than destroyed.

The low-security example reuses the run's bucket and instance profile, so it deploys after the private shape and is torn down before it:

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> example deploy
```

```sh
./bin/qualify-aws --profile <profile> --region <region> --run-id <id> example teardown
```

Evidence is written under `QUALIFICATION_HOME` (default `~/.biblios-qualification/<run-id>/`): the journal of every call (request parameters without bodies, and the identifiers each response carried), the recorded target, the per-stack state, the command invocation, the lane records, log and exit status, the inventory and the denylist of every identifier the run observed. `results` renders `briefs/aws-qualification-results.md` from that directory with every denylisted identifier absent by construction.

## What the private instance needs

The instance has no internet. Everything arrives in the archive: the repository at the pinned commit, the managed Python runtime, `uv`, a cache pre-seeded by `uv sync --locked` for the product and for the Vellric sub-project under `qualification/vellric` (Vellric is a Python command-line tool, so it is installed on the instance from the staged cache like the product and reached through a `bin/vellric` shim), the corpus and, when `--tools` names one, a directory of prebuilt tool trees for the instance's distribution, each executable under `<tree>/bin/` reached through a shim in the bundle's `bin/`. Palace's pinned cross-encoder reranker model travels in the bundle too, provisioned at build time by Palace's own tool and digest-checked by it, so search on the private instance reranks with the network disabled; the offline proof loads it before any instance exists. Three tools travel the tools route: git, which uv needs to materialize its git-pinned sources even from the staged cache and which the Amazon Linux 2023 image does not carry (the first live run failed at `uv sync` for exactly that reason); `file`, which Drawbridge uses to sniff media types; and Tesseract, which Amazon Linux 2023 does not package at all. Each tree is extracted or built in a container of the instance's distribution and architecture and carries the shared libraries and data it needs. The instance extracts the archive with modes and links preserved and installs both environments with the same `uv sync` lines the proof ran, capturing the installer's output and a report of the three tools in `install.log`; the development-only infra toolchain that `./bin/setup` also provisions is not part of the instance, and the instance's package manager is never asked for anything. Every result artifact that exists is uploaded from an exit trap, so a failure before the lanes still leaves its diagnostics in the run's results prefix.

```admonish info title="Action-time confirmations are the boundary"
The runner never decides to spend. A packet is shown, the operator types its token, and only then does the one named action run. A declined or absent token stops the run at that step and leaves what exists in place; a later `teardown` is the way to remove it.
```
