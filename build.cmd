@echo off
rem Dev build: Headroom.exe next to the sources (run installer\build.ps1 once first, for the WebView2 DLLs).
cd /d %~dp0
%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe /win32manifest:app.manifest /win32icon:headroom.ico /platform:x64 /r:Microsoft.Web.WebView2.Core.dll /r:Microsoft.Web.WebView2.WinForms.dll /out:Headroom.exe Headroom.cs
