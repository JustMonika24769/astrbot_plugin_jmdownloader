[CmdletBinding()]
param(
    [string]$OutputPath = "",
    [ValidateSet("zip", "7z")]
    [string]$ArchiveType = "zip",
    [string]$SevenZipPath = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$projectName = Split-Path $projectRoot -Leaf
$metadataPath = Join-Path $projectRoot "metadata.yaml"
$schemaPath = Join-Path $projectRoot "_conf_schema.json"

$versionLine = Get-Content -LiteralPath $metadataPath |
    Where-Object { $_ -match '^version:\s*(.+?)\s*$' } |
    Select-Object -First 1
if (-not $versionLine -or $versionLine -notmatch '^version:\s*(.+?)\s*$') {
    throw "metadata.yaml 缺少有效的 version 字段。"
}
$releaseVersion = $Matches[1].Trim().Trim('"', "'")
if (-not $releaseVersion.StartsWith("v")) {
    $releaseVersion = "v$releaseVersion"
}

Get-Content -LiteralPath $schemaPath -Raw | ConvertFrom-Json | Out-Null

$requiredFiles = @(
    "main.py",
    "jm_service.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md"
)
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $requiredFile))) {
        throw "缺少插件必需文件: $requiredFile"
    }
}

if (-not $SevenZipPath) {
    $command = Get-Command 7z -ErrorAction SilentlyContinue
    if ($command) {
        $SevenZipPath = $command.Source
    }
}

if (-not $SevenZipPath) {
    $candidates = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files\7-Zip\7zz.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe"
    )
    $SevenZipPath = $candidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
}

if (-not $SevenZipPath -or -not (Test-Path -LiteralPath $SevenZipPath)) {
    throw "找不到 7-Zip，请使用 -SevenZipPath 指定 7z.exe。"
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "找不到 Git。打包脚本需要 Git 根据 .gitignore 生成文件清单。"
}

if (-not $OutputPath) {
    $OutputPath = Join-Path $projectRoot "dist\$projectName-$releaseVersion.$ArchiveType"
} elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $projectRoot $OutputPath
}

$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path $OutputPath -Parent
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

$tempDirectory = Join-Path ([IO.Path]::GetTempPath()) "$projectName-package-$([guid]::NewGuid().ToString('N'))"
$listPath = Join-Path $tempDirectory "include.txt"
New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null

try {
    Push-Location $projectRoot
    try {
        # Tracked files plus untracked files that are not excluded by .gitignore.
        $files = @(& git -C $projectRoot ls-files --cached --others --exclude-standard)
        if ($LASTEXITCODE -ne 0) {
            throw "Git 无法根据 .gitignore 生成文件清单。"
        }
    } finally {
        Pop-Location
    }

    if ($files.Count -eq 0) {
        throw "没有找到可打包的文件。"
    }

    $developmentPatterns = @(
        '^tests/',
        '^\.github/',
        '^\.gitattributes$',
        '^\.gitignore$',
        '^pyproject\.toml$',
        '^requirements-dev\.txt$',
        '^scripts/package\.ps1$'
    )
    $files = @($files | Where-Object {
        $file = $_ -replace '\\', '/'
        -not ($developmentPatterns | Where-Object { $file -match $_ })
    })

    $secretPattern = '(?i)["'']?(AVS|remember|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token)["'']?\s*[:=]\s*["'']?(?!\.{3}|your[_-]|example|changeme|null|\{\})[^\s"''#,}]{8,}'
    foreach ($file in $files) {
        $sourcePath = Join-Path $projectRoot $file
        if ($file -match '\.(ya?ml|json|toml|env|ini|cfg)$') {
            $match = Select-String -LiteralPath $sourcePath -Pattern $secretPattern
            if ($match) {
                throw "检测到疑似凭据，已停止打包: ${file}:$($match.LineNumber)"
            }
        }
    }

    [IO.File]::WriteAllLines(
        $listPath,
        $files,
        [Text.UTF8Encoding]::new($false)
    )

    $arguments = @(
        "a",
        "-t$ArchiveType",
        "-scsUTF-8",
        "-mx=9",
        "-y",
        $OutputPath,
        "@$listPath"
    )

    Write-Host "使用 7-Zip: $SevenZipPath"
    Write-Host "输入文件数: $($files.Count)"
    Write-Host "输出文件: $OutputPath"

    Push-Location $projectRoot
    try {
        if (Test-Path -LiteralPath $OutputPath) {
            if ((Get-Item -LiteralPath $OutputPath) -is [IO.DirectoryInfo]) {
                throw "输出路径是目录，无法覆盖: $OutputPath"
            }
            Remove-Item -LiteralPath $OutputPath -Force
        }
        & $SevenZipPath @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "7-Zip 打包失败，退出码: $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }

    $archive = Get-Item -LiteralPath $OutputPath
    Write-Host "打包完成: $($archive.FullName) ($($archive.Length) bytes)"
} finally {
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force
    }
}
