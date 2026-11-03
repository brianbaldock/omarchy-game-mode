# Maintainer: Omarchy Game Mode contributors
pkgname=omarchy-game-mode
pkgver=0.1.0
pkgrel=1
pkgdesc="Controller-first Gaming Home and game store launcher for Omarchy"
arch=(any)
url="https://github.com/brianbaldock/omarchy-game-mode"
license=(MIT)
depends=(python python-gobject gtk4)
optdepends=(
  'hyprland: window tracking and game handoff'
  'uwsm: launch apps in their own systemd units'
  'antimicrox: switch controller profiles between desktop and game input'
  'steam: Steam Big Picture'
)
# Built from a local tarball: run `make dist` in the source tree first.
source=("$pkgname-$pkgver.tar.gz")
sha256sums=(SKIP)

check() {
  cd "$pkgname-$pkgver"
  make test
}

package() {
  cd "$pkgname-$pkgver"
  make install DESTDIR="$pkgdir" PREFIX=/usr
}
