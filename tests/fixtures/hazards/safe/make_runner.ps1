# writes a batch runner; the redirect leads each status line
$runner = @(
    '@echo off',
    'call step_one.cmd',
    '>>"status.txt" echo step_one=%ERRORLEVEL%'
)
Set-Content -Path runner.cmd -Value $runner
