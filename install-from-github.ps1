#!/usr/bin/env pwsh
# 一键从 GitHub Releases 下载最新应用商店安装包并部署到小米智能存储。
#
# 用法：
#   .\install-from-github.ps1
#   .\install-from-github.ps1 -NasIp 192.168.31.100
#
# 可选参数：
#   -NasIp          NAS 局域网 IP
#   -NasSshKey      root SSH 私钥路径
#   -NasUserId      小米用户 ID
#   -PluginId       商店插件编号，默认 11002
#   -GithubRepo     仓库 owner/name，默认 fw867/xiaomi-nas-plugin-market
#   -GithubToken    可选，私有仓库或提高 API 限速时使用

[CmdletBinding()]
param(
    [string]$NasIp = $env:NAS_IP,
    [string]$NasSshKey = $env:NAS_SSH_KEY,
    [string]$NasUserId = $env:NAS_USER_ID,
    [int]$PluginId = 11002,
    [string]$GithubRepo = $(if ($env:GITHUB_REPO) { $env:GITHUB_REPO } else { "fw867/xiaomi-nas-plugin-market" }),
    [string]$GithubToken = $env:GITHUB_TOKEN
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Write-Info([string]$msg)  { Write-Host "[信息] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg)  { Write-Host "[警告] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)   { Write-Host "[错误] $msg" -ForegroundColor Red }

# ---------- 依赖检查 ----------
foreach ($cmd in @("ssh.exe", "scp.exe")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Err "未找到 $cmd。请在 Windows 设置的可选功能中安装 OpenSSH 客户端。"
        exit 1
    }
}

$WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) "xiaomi-store-install-$PID"
New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null

try {
    # ---------- 1. 获取最新 Release ----------
    $Api = "https://api.github.com/repos/$GithubRepo"
    $headers = @{ Accept = "application/vnd.github+json" }
    if ($GithubToken) { $headers["Authorization"] = "Bearer $GithubToken" }

    Write-Info "查询 $GithubRepo 最新 Release …"
    try {
        $release = Invoke-RestMethod -Uri "$Api/releases/latest" -Headers $headers -TimeoutSec 30
    } catch {
        Write-Err "无法获取最新 Release：$_"
        exit 1
    }

    $Tag = $release.tag_name
    if (-not $Tag) { Write-Err "Release 无 tag_name。"; exit 1 }
    $Version = $Tag.TrimStart("v")
    Write-Info "最新版本：$Version（$Tag）"

    # ---------- 2. 找到资产 ----------
    $ZipName = "xiaomi-plugin-market-$Version.zip"
    $ShaName = "SHA256SUMS.txt"

    $zipAsset = $release.assets | Where-Object { $_.name -eq $ZipName } | Select-Object -First 1
    $shaAsset = $release.assets | Where-Object { $_.name -eq $ShaName } | Select-Object -First 1

    if (-not $zipAsset) {
        Write-Err "Release 中未找到 $ZipName。可用资产："
        $release.assets | ForEach-Object { Write-Host "  - $($_.name)  ($([int]($_.size/1024)) KiB)" }
        exit 1
    }
    Write-Info "下载地址：$($zipAsset.browser_download_url)"

    # ---------- 3. 下载 ----------
    Write-Info "下载安装包 …"
    $ZipPath = Join-Path $WorkDir $ZipName
    Invoke-WebRequest -Uri $zipAsset.browser_download_url -OutFile $ZipPath -TimeoutSec 300

    $ShaPath = $null
    if ($shaAsset) {
        Write-Info "下载校验文件 …"
        $ShaPath = Join-Path $WorkDir $ShaName
        Invoke-WebRequest -Uri $shaAsset.browser_download_url -OutFile $ShaPath -TimeoutSec 60
    } else {
        Write-Warn "Release 未附带 $ShaName，将跳过 SHA-256 校验。"
    }

    # ---------- 4. SHA-256 校验 ----------
    if ($ShaPath -and (Test-Path $ShaPath)) {
        Write-Info "校验 SHA-256 …"
        $hashLine = Get-Content $ShaPath | Where-Object { $_ -match [regex]::Escape($ZipName) } | Select-Object -First 1
        if (-not $hashLine) {
            Write-Err "$ShaName 中未找到 $ZipName 的哈希。"
            exit 1
        }
        $Expected = ($hashLine -split '\s+')[0]
        $Actual = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLower()
        if ($Expected -ne $Actual) {
            Write-Err "SHA-256 校验失败！"
            Write-Err "  期望：$Expected"
            Write-Err "  实际：$Actual"
            exit 1
        }
        Write-Info "SHA-256 校验通过：$($Actual.Substring(0,16))…"
    }

    # ---------- 5. 解压 ----------
    Write-Info "解压安装包 …"
    $ExtractDir = Join-Path $WorkDir "extracted"
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

    # 找到 deploy/install-on-nas.sh 所在目录
    $installScript = Get-ChildItem -Path $ExtractDir -Recurse -Filter "install-on-nas.sh" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "deploy" } | Select-Object -First 1
    if (-not $installScript) {
        Write-Err "解压后未找到 deploy/install-on-nas.sh"
        Get-ChildItem -Path $ExtractDir -Recurse -Depth 3 | Select-Object -First 20 FullName
        exit 1
    }
    $ProjectDir = $installScript.Directory.Parent.FullName
    Write-Info "项目目录：$ProjectDir"

    foreach ($required in @("server.py", "storelib.py", "web\index.html", "catalog\catalog.json", "deploy\install-on-nas.sh")) {
        if (-not (Test-Path (Join-Path $ProjectDir $required))) {
            Write-Err "发布包缺少文件：$required"
            exit 1
        }
    }
    Write-Info "发布包文件完整。"

    # ---------- 6. 收集 NAS 连接信息 ----------
    if (-not $NasIp) {
        $NasIp = Read-Host "请输入小米 NAS 当前局域网 IP"
    }
    $parsedIp = $null
    if (-not [System.Net.IPAddress]::TryParse($NasIp, [ref]$parsedIp) -or $parsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        Write-Err "NAS IP 格式不正确：$NasIp"
        exit 1
    }

    if (-not $NasSshKey) {
        $candidates = @(
            (Join-Path $HOME ".xiaomi-nas-root\nas-root-key"),
            (Join-Path $HOME ".ssh\xiaomi-nas-root"),
            (Join-Path $HOME ".ssh\nas-root-key")
        )
        $NasSshKey = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $NasSshKey) {
        $NasSshKey = Read-Host "未自动找到 SSH 密钥，请输入 root 私钥路径"
        $NasSshKey = $NasSshKey.Trim('"')
    }
    if (-not (Test-Path $NasSshKey)) {
        Write-Err "SSH 私钥不存在：$NasSshKey"
        exit 1
    }

    $Remote = "root@$NasIp"
    $SshOptions = @(
        "-i", $NasSshKey,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PubkeyAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new"
    )

    Write-Info "测试 SSH 连接 $Remote …"
    $testResult = & ssh.exe @SshOptions $Remote "echo ok" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "无法 SSH 到 $Remote。请检查 IP、密钥和 NAS 是否已开启密钥 SSH。"
        exit 1
    }
    Write-Info "SSH 连接成功。"

    # ---------- 7. 检测小米用户 ID ----------
    if (-not $NasUserId) {
        Write-Info "检测小米用户 ID …"
        $registryOutput = & ssh.exe @SshOptions $Remote 'for path in /data/plugin/u*.list; do [ -f "$path" ] || continue; name=${path##*/}; printf "%s\n" "${name%.list}"; done'
        $users = @($registryOutput | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^u[A-Za-z0-9_-]+$' } | Select-Object -Unique)
        if ($users.Count -eq 1) {
            $NasUserId = $users[0]
        } elseif ($users.Count -gt 1) {
            Write-Host "检测到多个小米账号："
            for ($i = 0; $i -lt $users.Count; $i++) { Write-Host "  $($i+1). $($users[$i])" }
            $sel = Read-Host "请输入当前账号前面的序号"
            $idx = [int]$sel - 1
            if ($idx -lt 0 -or $idx -ge $users.Count) { Write-Err "序号无效"; exit 1 }
            $NasUserId = $users[$idx]
        } else {
            Write-Err "无法自动确定小米账号。请设置 NAS_USER_ID 后重试。"
            exit 1
        }
    }
    if ($NasUserId -notmatch '^u[A-Za-z0-9_-]+$') {
        Write-Err "小米用户 ID 无效：$NasUserId"
        exit 1
    }
    Write-Info "使用小米用户：$NasUserId"

    # ---------- 8. 通过 SSH 执行安装 ----------
    Write-Info "上传安装脚本并执行 …"
    $ScpOptions = $SshOptions
    $sshVersionOutput = (& cmd.exe /c "ssh.exe -V 2>&1" | Out-String)
    if ($sshVersionOutput -match 'OpenSSH(?:_for_Windows)?_(\d+)' -and [int]$Matches[1] -ge 9) {
        $ScpOptions += "-O"
    }

    # 上传整个项目目录到 NAS 临时位置
    $RemoteTmp = "/tmp/xiaomi-store-install-$$"
    & ssh.exe @SshOptions $Remote "rm -rf '$RemoteTmp' && mkdir -p '$RemoteTmp'"
    if ($LASTEXITCODE -ne 0) { Write-Err "创建远程临时目录失败"; exit 1 }

    # 使用 scp 上传关键目录
    foreach ($item in @("server.py", "storelib.py", "web", "catalog", "deploy")) {
        $source = Join-Path $ProjectDir $item
        & scp.exe @ScpOptions -r $source "${Remote}:$RemoteTmp/"
        if ($LASTEXITCODE -ne 0) { Write-Err "上传 $item 失败"; exit 1 }
    }

    # 在 NAS 上执行安装
    $env:NAS_IP = $NasIp
    $env:NAS_SSH_KEY = $NasSshKey
    $env:NAS_USER_ID = $NasUserId
    $env:PLUGIN_ID = $PluginId

    & ssh.exe @SshOptions $Remote "export NAS_USER_ID='$NasUserId' PLUGIN_ID='$PluginId' NAS_IP='$NasIp'; bash '$RemoteTmp/deploy/install-on-nas.sh'"
    if ($LASTEXITCODE -ne 0) { Write-Err "NAS 上安装失败"; exit 1 }

    # 清理远程临时目录
    & ssh.exe @SshOptions $Remote "rm -rf '$RemoteTmp'" 2>$null

    Write-Host ""
    Write-Info "安装完成！请完全退出并重新打开小米智能存储客户端。"
    Write-Info "应用商店会使用小米客户端入口自动授权，不需要管理码。"

} finally {
    Remove-Item -Path $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
}
