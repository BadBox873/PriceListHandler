#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{742C6989-6872-4E20-80A8-2E12A655F4A1}
AppName=PriceListHandler
AppVersion={#AppVersion}
AppPublisher=BadBox873
AppPublisherURL=https://github.com/BadBox873/PriceListHandler
AppSupportURL=https://github.com/BadBox873/PriceListHandler/issues
DefaultDirName={localappdata}\Programs\PriceListHandler
DefaultGroupName=PriceListHandler
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=PriceListHandler-{#AppVersion}-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PriceListHandler.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "..\dist\PriceListHandler\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PriceListHandler"; Filename: "{app}\PriceListHandler.exe"
Name: "{group}\Удалить PriceListHandler"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PriceListHandler"; Filename: "{app}\PriceListHandler.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PriceListHandler.exe"; Description: "Запустить PriceListHandler"; Flags: nowait postinstall skipifsilent

; User databases, drafts and backups are outside the installation and preserved.
