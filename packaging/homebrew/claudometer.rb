# Homebrew Cask. The version + sha256 are filled by packaging/update-manifests.py
# after a release. Publish via your own tap repo (homebrew-claudometer), then:
#   brew install --cask ali-dev178/claudometer/claudometer
cask "claudometer" do
  version "1.1.13"
  sha256 "71c151ebc819cf3e023cfdbe5f435c73c48674d64ea66a550debc072b71e4c98"

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
