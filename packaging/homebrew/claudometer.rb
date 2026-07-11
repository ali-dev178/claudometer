# Homebrew Cask. The version + sha256 are filled by packaging/update-manifests.py
# after a release. Publish via your own tap repo (homebrew-claudometer), then:
#   brew install --cask ali-dev178/claudometer/claudometer
cask "claudometer" do
  version "1.1.12"
  sha256 "4258001f467694eb3e12be387dc8828747834dee5f426c180bbbd4c28a0f753f"

  url "https://github.com/ali-dev178/claudometer/releases/download/v#{version}/Claudometer-macos.zip"
  name "Claudometer"
  desc "Live Claude usage limits (session & weekly) in your menu bar"
  homepage "https://github.com/ali-dev178/claudometer"

  app "Claudometer.app"

  caveats <<~EOS
    Claudometer reads your local Claude Code credentials and shows usage in the
    macOS menu bar. Requires a Claude Pro/Max/Team subscription signed into Claude Code.

    This is an unsigned build: on first launch macOS may block it. Right-click the
    app and choose "Open", or allow it in System Settings > Privacy & Security.
  EOS
end
