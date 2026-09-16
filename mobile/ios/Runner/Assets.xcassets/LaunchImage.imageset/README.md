# Launch Screen Assets

`LaunchImage.png`, `LaunchImage@2x.png` and `LaunchImage@3x.png` are the
FeohLedger brand mark at 64pt (64, 128 and 192 px), rendered from
`assets/logo-mark.svg` by `assets/gen-icons.sh` (`pnpm gen:icons`). Don't replace
them by hand or by dropping images in Xcode: change the master, re-run the
script, then run `pnpm check:icons`.

`Base.lproj/LaunchScreen.storyboard` shows this image 26pt above the screen
centre on `#F8F9FF`. That is where, and on what colour, Flutter's `SplashScreen`
(`lib/main.dart`) draws the same mark, so the hand-off doesn't move or recolour
it. See `mobile/docs/project-structure.md` § Launch screens.
