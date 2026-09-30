import QtQuick
import QtQuick.Controls
import QtQuick.Window

/**
 * Root window — 1280×800, frameless, fullscreen.
 *
 * Everything visual lives in the active style's Shell.qml, loaded through the
 * single Loader below. `stylePath` is set once at boot by main.py, so exactly
 * one style is ever parsed or instantiated — an inactive style costs nothing.
 */
ApplicationWindow {
    id: window
    width: 1280
    height: 800
    visible: true
    flags: Qt.FramelessWindowHint
    color: (Theme && Theme.ground) ? Theme.ground : "#0e0f11"
    title: "LaLaDash"

    // [UI] fullscreen was only ever applied by the Settings toggle at runtime,
    // so on the Pi the app came up as a plain window under the desktop panel
    // and lost the strip the panel occupied. Applied once here rather than as
    // a binding on `visibility`, because a binding fights the bridge's own
    // showFullScreen()/showNormal() calls the moment the toggle is used.
    Component.onCompleted: {
        if (typeof Settings !== "undefined" && Settings && Settings.fullscreenEnabled)
            window.showFullScreen();
    }

    FontLoader { id: fontRegular;  source: fontsPath + "/Manrope-Regular.ttf" }
    FontLoader { id: fontMedium;   source: fontsPath + "/Manrope-Medium.ttf" }
    FontLoader { id: fontSemiBold; source: fontsPath + "/Manrope-SemiBold.ttf" }
    FontLoader { id: fontBold;     source: fontsPath + "/Manrope-Bold.ttf" }

    Loader {
        id: shellLoader
        anchors.fill: parent
        asynchronous: false
        source: stylePath + "Shell.qml"
        onStatusChanged: {
            if (status === Loader.Error)
                console.error("Failed to load style shell:", source);
        }
    }
}
