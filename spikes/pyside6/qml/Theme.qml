import QtQuick 6.5

QtObject {
    // Keep these values aligned with app.py's compact black-and-white shell.
    readonly property color card: "#0a0a0a"
    readonly property color resultSurface: "#050505"
    readonly property color border: "#1c1c1c"
    readonly property color text: "#ffffff"
    readonly property color secondaryText: "#cccccc"
    readonly property color dim: "#666666"
    readonly property color control: "#151515"
    readonly property color controlHover: "#222222"
    readonly property color controlDisabled: "#101010"
    readonly property color transparentKey: "#010101"

    readonly property int pillRadius: 24
    readonly property int panelRadius: 18
    readonly property int controlRadius: 13
    readonly property int controlHeight: 26
    readonly property int windowWidth: 380
    readonly property int windowHeight: 48
    readonly property int resultWidth: 400
    readonly property int resultHeight: 148
    readonly property int settingsHeight: 204
}
