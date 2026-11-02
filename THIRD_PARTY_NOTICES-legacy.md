# Third-party notices

This working folder contains separate third-party runtimes and model files. Their upstream licenses remain applicable.

- ComfyUI 0.36.0: GPL-3.0. Source is preserved under `backends/ComfyUI`; its repository license and notices travel with that source.
- ComfyUI-GGUF commit `6ea2651e7df66d7585f6ffee804b20e92fb38b8a`: Apache-2.0. Source and license are preserved under the ComfyUI custom-nodes folder.
- ComfyUI-Impact-Pack commit `429d0159ad429e64d2b3916e6e7be9c22d025c3c`: GPL-3.0. Source and license are preserved under the ComfyUI custom-nodes folder.
- ComfyUI-Impact-Subpack commit `50c7b71a6a224734cc9b21963c6d1926816a97f1`: AGPL-3.0. Source and license are preserved under the ComfyUI custom-nodes folder.
- Face, hand, and person detector weights from `Bingsu/adetailer` revision `53cc19de382014514d9d4038601d261a7faa9b7b`: Apache-2.0. Exact hashes are recorded in `MODEL_MANIFEST.json`.
- Ollama portable 0.34.1 and its bundled llama.cpp runtime: upstream notices are preserved under `backends/ollama/lib/ollama`.
- Model provenance, hashes, quantization author, and commercial-use decision are recorded in `MODEL_MANIFEST.json`.

The earlier Comfy-Org `flux2-dev` VAE was rejected because that repository is marked non-commercial. Its bytes were removed from the active model directory and replaced by the official Apache-2.0 VAE from `black-forest-labs/FLUX.2-klein-4B`.
