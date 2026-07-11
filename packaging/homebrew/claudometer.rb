# Homebrew Cask. The version + sha256 are filled by packaging/update-manifests.py
# after a release. Publish via your own tap repo (homebrew-claudometer), then:
#   brew install --cask ali-dev178/claudometer/claudometer
cask "claudometer" do
  version "1.1.8"
  sha256 "f27a92c143563e52886d66fa385a6b7121027e32997b1ec73c565fa3d2324a42"

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
