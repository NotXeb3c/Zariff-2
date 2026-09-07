cask "zariff" do
  version "0.2.0"
  sha256 :no_check # Will be filled after release

  url "https://github.com/NotXeb3c/zariff-2/releases/download/v#{version}/Zariff_#{version}_aarch64.dmg",
      verified: "github.com/NotXeb3c/zariff-2/"

  name "Zariff"
  desc "AI System Control Agent — control your computer with voice, text, and gestures"
  homepage "https://github.com/NotXeb3c/zariff-2"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on formula: "python@3.12"

  app "Zariff.app"

  zap trash: [
    "~/.config/zariff",
    "~/Library/Application Support/com.zariff.app",
    "~/Library/Application Support/com.helioxos.app",
  ]
end
