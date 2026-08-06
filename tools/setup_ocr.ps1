param(
    [ValidateSet("Auto", "Gpu", "Cpu")]
    [string]$Backend = "Auto",
    [ValidatePattern("^(?i:Auto|cu\d{3,4})$")]
    [string]$Cuda = "Auto",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$OcrRequirements = Join-Path $ProjectRoot "requirements-ocr.txt"
$PaddleVersion = "3.3.0"
$PaddleStableRoot = "https://www.paddlepaddle.org.cn/packages/stable/"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv。请先在项目目录运行：uv venv --python 3.13"
}

$PythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$PythonVersion -gt [version]"3.13") {
    throw "PaddlePaddle 3.3.0 暂无 Python $PythonVersion 轮子，请使用 Python 3.9-3.13。"
}
if ([version]$PythonVersion -lt [version]"3.9") {
    throw "PP-StructureV3 文档解析依赖需要 Python 3.9-3.13。"
}

function Get-NvidiaCudaVersion {
    $NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $NvidiaSmi) {
        return $null
    }
    $Output = (& $NvidiaSmi.Source 2>$null) -join "`n"
    $Match = [regex]::Match($Output, "CUDA(?: UMD)? Version:\s*(\d+\.\d+)")
    if ($Match.Success) {
        return [version]$Match.Groups[1].Value
    }
    return $null
}

function ConvertFrom-CudaTag {
    param([string]$CudaTag)

    $Digits = $CudaTag.Substring(2)
    $Major = $Digits.Substring(0, $Digits.Length - 1)
    $Minor = $Digits.Substring($Digits.Length - 1)
    return [version]"$Major.$Minor"
}

function Invoke-PaddleWebRequest {
    param([string]$Uri)

    $PythonCode = @"
import sys
import time
import urllib.request

last_error = None
for attempt in range(3):
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=30) as response:
            sys.stdout.buffer.write(response.read())
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        if attempt < 2:
            time.sleep(1)
print(last_error, file=sys.stderr)
raise SystemExit(1)
"@
    $Content = (& $Python -c $PythonCode $Uri) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        throw "访问 $Uri 失败。"
    }
    return $Content
}

function Get-OfficialCudaBuilds {
    try {
        $ResponseContent = Invoke-PaddleWebRequest $PaddleStableRoot
    } catch {
        throw "无法读取 PaddlePaddle 官方 CUDA 软件源：$($_.Exception.Message)"
    }

    return [regex]::Matches($ResponseContent, "/packages/stable/(cu\d+)/") |
        ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() } |
        Sort-Object -Unique |
        Sort-Object { ConvertFrom-CudaTag $_ } -Descending
}

$CudaBuildCompatibility = @{}
function Test-CompatibleCudaBuild {
    param([string]$CudaTag)

    if ($CudaBuildCompatibility.ContainsKey($CudaTag)) {
        return $CudaBuildCompatibility[$CudaTag]
    }

    $PythonTag = "cp$($PythonVersion.Replace('.', ''))"
    $PackageUri = "$PaddleStableRoot$CudaTag/paddlepaddle-gpu/"
    try {
        $ResponseContent = Invoke-PaddleWebRequest $PackageUri
        $EscapedVersion = [regex]::Escape($PaddleVersion)
        $WheelPattern = "paddlepaddle_gpu-$EscapedVersion-$PythonTag-$PythonTag-win_amd64\.whl"
        $Compatible = [regex]::IsMatch($ResponseContent, $WheelPattern)
    } catch {
        $Compatible = $false
    }

    $CudaBuildCompatibility[$CudaTag] = $Compatible
    return $Compatible
}

function Test-PythonDistribution {
    param([string]$DistributionName)

    $Exists = & $Python -c `
        "import importlib.metadata as m, sys; sys.stdout.write('1' if any((d.metadata.get('Name') or '').lower() == sys.argv[1].lower() for d in m.distributions()) else '0')" `
        $DistributionName
    return ($LASTEXITCODE -eq 0 -and $Exists -eq "1")
}

function Get-InstalledPaddleCuda {
    $VersionFile = Join-Path $ProjectRoot ".venv\Lib\site-packages\paddle\version\__init__.py"
    if (-not (Test-Path -LiteralPath $VersionFile)) {
        return $null
    }
    $VersionText = Get-Content -LiteralPath $VersionFile -Raw
    $VersionMatch = [regex]::Match(
        $VersionText,
        '(?m)^cuda_version\s*=\s*[''"]([^''"]+)'
    )
    if (-not $VersionMatch.Success) {
        return $null
    }
    return $VersionMatch.Groups[1].Value
}

function Add-NvidiaDllDirectoriesToPath {
    param([string]$CudaTag)

    $NvidiaRoot = Join-Path $ProjectRoot ".venv\Lib\site-packages\nvidia"
    if (-not (Test-Path -LiteralPath $NvidiaRoot)) {
        return
    }
    $CudaMajorDirectory = "cu$((ConvertFrom-CudaTag $CudaTag).Major)"
    $DllDirectories = @(
        Get-ChildItem -LiteralPath $NvidiaRoot -Recurse -Filter *.dll |
            ForEach-Object { $_.DirectoryName } |
            Sort-Object -Unique
    )
    $PreferredDirectories = @(
        $DllDirectories | Where-Object {
            $_ -match "[\\/]$([regex]::Escape($CudaMajorDirectory))[\\/]"
        }
    )
    $OtherDirectories = @(
        $DllDirectories | Where-Object { $_ -notin $PreferredDirectories }
    )
    $env:PATH = (@($PreferredDirectories) + @($OtherDirectories) + @($env:PATH)) -join `
        [IO.Path]::PathSeparator
    Write-Host "已为当前进程配置 $($DllDirectories.Count) 个 NVIDIA DLL 目录。"
}

$CudaVersion = Get-NvidiaCudaVersion
if ($Backend -eq "Auto") {
    $Backend = if ($CudaVersion) { "Gpu" } else { "Cpu" }
}

if ($Backend -eq "Gpu") {
    $OfficialCudaBuilds = @(Get-OfficialCudaBuilds)
    $LatestCuda = $null
    foreach ($Candidate in $OfficialCudaBuilds) {
        if (Test-CompatibleCudaBuild $Candidate) {
            $LatestCuda = $Candidate
            break
        }
    }
    if (-not $LatestCuda) {
        throw "官方软件源中没有适用于 PaddlePaddle $PaddleVersion、Python $PythonVersion、Windows x64 的 CUDA wheel。"
    }

    Write-Host "检测到当前环境最新可用版本：$LatestCuda（PaddlePaddle $PaddleVersion / Python $PythonVersion / Windows x64）"

    if ($Cuda -eq "Auto") {
        if (-not $CudaVersion) {
            throw "未检测到 NVIDIA CUDA 驱动。可使用 -Backend Cpu，或明确指定 -Cuda。"
        }
        foreach ($Candidate in $OfficialCudaBuilds) {
            if (
                (ConvertFrom-CudaTag $Candidate) -le $CudaVersion -and
                (Test-CompatibleCudaBuild $Candidate)
            ) {
                $Cuda = $Candidate
                break
            }
        }
        if ($Cuda -eq "Auto") {
            throw "本机驱动最高支持 CUDA $CudaVersion，但没有找到相容的官方 PaddlePaddle wheel；当前环境最新版本为 $LatestCuda。"
        }
    } else {
        $Cuda = $Cuda.ToLowerInvariant()
        if (-not (Test-CompatibleCudaBuild $Cuda)) {
            throw "官方尚未提供适用于 PaddlePaddle $PaddleVersion、Python $PythonVersion、Windows x64 的 $Cuda wheel；当前环境最新版本为 $LatestCuda。"
        }
    }

    Write-Host "本次选择：$Cuda"
    $HasCpuPackage = Test-PythonDistribution "paddlepaddle"
    $HasGpuPackage = Test-PythonDistribution "paddlepaddle-gpu"
    $InstalledCuda = if ($HasGpuPackage) { Get-InstalledPaddleCuda } else { $null }
    $TargetCuda = "$(ConvertFrom-CudaTag $Cuda)"
    $ForceReinstall = $HasCpuPackage -or ($HasGpuPackage -and $InstalledCuda -ne $TargetCuda)

    if ($CheckOnly) {
        if ($ForceReinstall) {
            $PreviousBackend = if ($InstalledCuda) { "CUDA $InstalledCuda" } else { "CPU" }
            Write-Host ("检测结果：已安装 {0}；正式执行时会替换为 CUDA {1}。" -f $PreviousBackend, $TargetCuda)
        } elseif ($HasGpuPackage) {
            Write-Host ("检测结果：CUDA {0} 已安装，无需切换。" -f $InstalledCuda)
        } else {
            Write-Host "检测结果：尚未安装 PaddlePaddle GPU；正式执行时会安装 CUDA $TargetCuda。"
        }
        Write-Host "仅检测，不安装。"
        return
    }

    if ($HasCpuPackage) {
        Write-Host "移除已安装的 PaddlePaddle CPU 后端..."
        & uv pip uninstall --python $Python paddlepaddle
        if ($LASTEXITCODE -ne 0) {
            throw "PaddlePaddle CPU 后端移除失败，退出码：$LASTEXITCODE"
        }
    }

    if ($ForceReinstall) {
        $PreviousBackend = if ($InstalledCuda) { $InstalledCuda } else { "CPU" }
        Write-Host "将 PaddlePaddle GPU 后端从 $PreviousBackend 切换到 $TargetCuda..."
    } else {
        Write-Host "安装 PaddlePaddle GPU $PaddleVersion（$Cuda）..."
    }
    $InstallArgs = @(
        "pip", "install", "--python", $Python,
        "paddlepaddle-gpu==$PaddleVersion",
        "--index-url", "$PaddleStableRoot$Cuda/"
    )
    if ($ForceReinstall) {
        $InstallArgs += @("--reinstall-package", "paddlepaddle-gpu")
    }
    & uv @InstallArgs
} else {
    if ($CheckOnly) {
        Write-Host "本次选择：CPU"
        if (Test-PythonDistribution "paddlepaddle-gpu") {
            Write-Host "检测结果：已安装 GPU 后端；正式执行时会替换为 CPU 后端。"
        } elseif (Test-PythonDistribution "paddlepaddle") {
            Write-Host "检测结果：CPU 后端已安装，无需切换。"
        } else {
            Write-Host "检测结果：尚未安装 PaddlePaddle；正式执行时会安装 CPU 后端。"
        }
        Write-Host "仅检测，不安装。"
        return
    }

    $HasGpuPackage = Test-PythonDistribution "paddlepaddle-gpu"
    if ($HasGpuPackage) {
        Write-Host "移除已安装的 PaddlePaddle GPU 后端..."
        & uv pip uninstall --python $Python paddlepaddle-gpu
        if ($LASTEXITCODE -ne 0) {
            throw "PaddlePaddle GPU 后端移除失败，退出码：$LASTEXITCODE"
        }
    }
    Write-Host "安装 PaddlePaddle CPU $PaddleVersion..."
    $CpuInstallArgs = @(
        "pip", "install", "--python", $Python,
        "paddlepaddle==$PaddleVersion",
        "--index-url", "${PaddleStableRoot}cpu/"
    )
    if ($HasGpuPackage) {
        $CpuInstallArgs += @("--reinstall-package", "paddlepaddle")
    }
    & uv @CpuInstallArgs
}
if ($LASTEXITCODE -ne 0) {
    throw "PaddlePaddle 安装失败，退出码：$LASTEXITCODE"
}

if ($Backend -eq "Gpu") {
    Add-NvidiaDllDirectoriesToPath $Cuda
}

Write-Host "安装 PaddleOCR 3.7 文档解析组件..."
& uv pip install --python $Python --requirement $OcrRequirements
if ($LASTEXITCODE -ne 0) {
    throw "PaddleOCR 安装失败，退出码：$LASTEXITCODE"
}

Write-Host "验证 OCR 环境..."
& $Python -c "import paddle, paddleocr; paddle.utils.run_check(); print('PaddleOCR', paddleocr.__version__, 'device', paddle.device.get_device())"
if ($LASTEXITCODE -ne 0) {
    throw "OCR 环境验证失败，退出码：$LASTEXITCODE"
}

Write-Host "PDF OCR 可选功能安装完成。"
