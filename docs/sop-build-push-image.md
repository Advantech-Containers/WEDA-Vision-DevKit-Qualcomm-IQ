# Build and Release Guide

**Contents**

- [1. Overview](#1-overview)
- [2. Prerequisites](#2-prerequisites)
- [3. Prepare the model and the clip](#3-prepare-the-model-and-the-clip)
- [4. Build](#4-build)
- [5. Check and scan](#5-check-and-scan)
- [6. Push and release](#6-push-and-release)
- [7. Files of the repository](#7-files-of-the-repository)

## 1. Overview

The demo has two images of its own: the judge (`vlm-judge`, built from this repository) and the Sub-Node (`vlm-judge-subnode`, built from the example `examples/vlm-judge-subnode` in the WEDA Sub-Node SDK repository, branch `feature/vlm-judge-subnode`). Both are arm64, so build on the AIR-055 or another arm64 machine, where nothing is emulated. The images go to a private registry project; its address is the one used in `docker/weda-stack-vlm-demo.yml`. In the commands below, `$REGISTRY` stands for that address up to the project name.

The judge image contains the Gemma 4 model and the demo clip, so a deployment needs nothing on the board. Both come from a folder outside the repository, `vlm-ppe-demo-assets/`, which keeps the repository small.

## 2. Prerequisites

- Docker on arm64 (`docker version`).
- The VLM container image used as the judge's base (`docker images | grep multimodal-gemma4`). The build pulls it if it is missing; it is in a private registry project, so log in first.
- A clone of the SDK repository on the branch `feature/vlm-judge-subnode`, for the Sub-Node.
- About 15 GB of free disk. The model exists three times during a build (assets folder, build context, image), and the free space dips by about 13 GB. Run `docker builder prune -f` afterwards.

## 3. Prepare the model and the clip

Once, before the first build. The folder `vlm-ppe-demo-assets/` sits next to the repository (any other folder works if you name it in the build command) and holds:

- `model-seed/`: the model, about 4 GB
- `clips/OD_Jar.mp4`: the demo clip, 14 MB

Make `model-seed/` from a board whose model volume is complete:

```bash
mkdir -p ../vlm-ppe-demo-assets/model-seed && docker run --rm -v vlm-geniex-data:/data:ro -v "$PWD/../vlm-ppe-demo-assets/model-seed":/out busybox sh -c 'cd /data && cp -a models aihub plugin_versions.json /out/ && rm -f /out/models/google/*/.lock /out/models/google/*/*.bf16.orig'
```

The judge does not convert anything, so the vision encoder in this folder must already be the converted F16 file. A model pulled freshly with `geniex pull` has the original BF16 file; convert it once with `tools/optimize-vision-encoder.sh` of the Multimodal Gemma 4 project before copying.

## 4. Build

**Judge image**, in this repository:

```bash
docker build -f docker/Dockerfile.vlm-judge --build-context assets=../vlm-ppe-demo-assets -t vlm-judge:0.13.3 .
```

The Dockerfile adds `paho-mqtt` (pinned) and `src/vlm_judge` to the base image; nothing is installed at run time. `BASE_IMAGE` selects the base (the default is the tag `v1`). Never reuse a tag that was pushed: every build gets a new one.

**Sub-Node image**, in the SDK clone:

```bash
PLATFORMS=linux/arm64 PUSH=false IMAGE_REPO=$REGISTRY/vlm-judge-subnode IMAGE_TAG=0.7.0 \
  scripts/build-push-image.sh vlm-judge-subnode
```

The image carries the WedaNode login. Set `WEDA_NATS_PASSWORD` in your shell to the board's WedaNode password for this one command (it is the `NATS_PASSWORD` of the `dmagent` container); the SDK's build guide, `09-build-push-deploy-images.md`, describes it. Do not write the value into any file. Anyone who can pull the image can read it, so keep the registry project private.

## 5. Check and scan

Check that the image starts and has the expected version:

```bash
docker run --rm --entrypoint python3 vlm-judge:0.13.3 -c "import vlm_judge; print(vlm_judge.__version__)"
```

Scan each image for known vulnerabilities with Trivy, run as a container on a saved image:

```bash
docker save $REGISTRY/vlm-judge:0.13.3 -o judge.tar
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/work aquasec/trivy:latest image --cache-dir /work/cache \
  --input /work/judge.tar --scanners vuln --severity HIGH,CRITICAL
```

The judge image inherits findings from its base image, almost all in `linux-libc-dev` (kernel headers, not used at run time). Look at the packages we add and at anything new since the last release.

## 6. Push and release

```bash
docker login <registry host> -u <user>                                        # type the password yourself
docker tag vlm-judge:0.13.3 $REGISTRY/vlm-judge:0.13.3   # the SDK script already tagged the Sub-Node image
docker push $REGISTRY/vlm-judge:0.13.3
docker push $REGISTRY/vlm-judge-subnode:0.7.0
docker logout <registry host>
```

Check the result with `docker buildx imagetools inspect $REGISTRY/vlm-judge:0.13.3`: it must list `linux/arm64`. The first push of the judge image is about 4 GB; later builds that change only the code push only small layers, because the model layer comes first in the Dockerfile.

Never push an existing tag again: a tag that moves breaks rollback.

Then write each image's version into `docker/weda-stack-vlm-demo.yml` (`image: <registry>/vlm-judge:0.13.3`), make sure the organisation has a registry entry with a pull credential in WEDA container management, and deploy as described in the [deployment guide](sop-run-on-edge-device.md). The stack file names the release. Older tags stay in the registry for rollback.

## 7. Files of the repository

| File | What it does | Why it is separate |
|---|---|---|
| `docker/weda-stack-vlm-demo.yml` | The WEDA stack: model server, judge, broker and Sub-Node, with the image versions | The file you upload in the WEDA portal |
| `docker/Dockerfile.vlm-judge` | Builds the judge image: code, model and demo clip on the VLM container image | Keeps the repository small: the model and clip come from a folder outside it |
| `src/vlm_judge/runner.py` | Starts the judge and connects camera, model, engine, MQTT, web view and API | The only place that wires the parts together |
| `src/vlm_judge/engine.py` | One judgement cycle: ask the model, check the answer, re-check a violation, watchdog | Takes the model call and the publisher as arguments, so it is tested without hardware |
| `src/vlm_judge/answer.py` | The prompts, the strict check of the model's JSON answer, and the verdict computed from it | The model only answers per person; counts and `compliant` are computed here |
| `src/vlm_judge/client.py` | Client of the model server (GenieX, OpenAI-style requests) | Classifies failures by type, so the engine counts timeouts and server errors correctly |
| `src/vlm_judge/errors.py` | The failure types of a model request | Shared by the client that raises them and the engine that counts them |
| `src/vlm_judge/mqtt.py` | The MQTT messages (contract 1.0) and the publisher | Holds the contract the Sub-Node reads; a failed publish is counted, never fatal |
| `src/vlm_judge/config.py` | Reads the settings from environment variables and rejects invalid values | An invalid value stops the start with a message naming the variable |
| `src/vlm_judge/webview.py` | The live picture on port 8080: the model's description and a state border (the top-left block is optional) | Helps a demo; if it fails the judge keeps running |
| `src/vlm_judge/api.py` | The optional read-only HTTP API (off by default; `API_PORT` turns it on) | Lets another program use the results without an MQTT client |
| `src/vlm_judge/__init__.py`, `__main__.py` | The version, and `python3 -m vlm_judge` | |
| `docs/` | The documents listed above, and `docs/images` | |
