# Homebrew Cask TEMPLATE. Fill <...>, host the zipped .app on a GitHub Release,
# then publish via your own tap:  brew install <you>/tap/claudometer
cask "claudometer" do
  version "0.1.0"
  sha256 "<SHA256_OF_ZIP>"

  url "https://github.com/<you>/claudometer/releases/download/v#{version}/Claudometer-macos.zip"
  name "Claudometer"
  desc "Live Claude usage limits in your menu bar"
  homepage "https://github.com/<you>/claudometer"

  app "Claudometer.app"

  caveats <<~EOS
    Claudometer reads your local Claude Code credentials and shows usage in the
    menu bar. Requires a Claude Pro/Max/Team subscription signed into Claude Code.
  EOS
end
