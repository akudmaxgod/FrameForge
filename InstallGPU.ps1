$ErrorActionPreference = 'Stop'
$applicationRoot = $PSScriptRoot
$applicationPython = Join-Path $applicationRoot '.venv/Scripts/python.exe'
$detectorConfig = Get-Content -LiteralPath (Join-Path $applicationRoot 'backends/detector-runtime.json') -Raw | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $applicationPython)) { throw 'Create the application environment first.' }
& $applicationPython -m pip --python $detectorConfig.python install --upgrade 'torch==2.11.0+cu128' 'torchvision==0.26.0+cu128' --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw 'CUDA detector installation failed.' }
& $applicationPython -m pip install -r (Join-Path $applicationRoot 'requirements-gpu.txt')
if ($LASTEXITCODE -ne 0) { throw 'CUDA pixel runtime installation failed.' }
$modelDirectory = Join-Path $applicationRoot 'models/semantic-mini'
$specPath = Join-Path $modelDirectory 'MODEL.json'
if (Test-Path -LiteralPath $specPath) {
    $spec = Get-Content -LiteralPath $specPath -Raw | ConvertFrom-Json
    $gpuModel = Join-Path $modelDirectory 'onnx/model-cuda.onnx'
    if (-not (Test-Path -LiteralPath $gpuModel)) {
        $modelUrl = "https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/resolve/$($spec.revision)/onnx/model.onnx"
        Invoke-WebRequest -Uri $modelUrl -OutFile $gpuModel
    }
    $spec.file = 'onnx/model-cuda.onnx'
    $hashes = @{}
    foreach ($property in $spec.sha256.PSObject.Properties) {
        if ($property.Name -notlike '*quant*') { $hashes[$property.Name] = $property.Value }
    }
    $hashes['onnx/model-cuda.onnx'] = (Get-FileHash -LiteralPath $gpuModel -Algorithm SHA256).Hash.ToLowerInvariant()
    $spec.sha256 = $hashes
    $spec | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $specPath -Encoding UTF8
}
& $applicationPython -c "from imagegen.gpu_pixels import cuda; print(cuda().cuda.runtime.getDeviceProperties(0)['name'])"
if ($LASTEXITCODE -ne 0) { throw 'CUDA capability check failed. CPU fallback is disabled.' }
