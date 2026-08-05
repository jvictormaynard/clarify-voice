import QtQuick 6.5
import QtQuick.Controls 6.5

Button {
    id: control
    required property Theme theme
    property bool primary: false
    property bool quiet: false

    implicitHeight: 44

    contentItem: Label {
        text: control.text
        color: !control.enabled
               ? control.theme.muted
               : control.primary ? "white" : control.theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        font.pixelSize: control.quiet ? 12 : 14
        font.weight: control.primary ? Font.Medium : Font.Normal
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: control.theme.radiusSmall
        color: !control.enabled
               ? control.theme.surfaceRaised
               : control.quiet ? "transparent"
               : control.primary ? control.theme.accentStrong
               : control.theme.surfaceSoft
        border.width: control.quiet ? 0 : 1
        border.color: !control.enabled
                      ? control.theme.border
                      : control.primary ? control.theme.accent
                      : control.theme.border

        Behavior on color {
            ColorAnimation { duration: 140; easing.type: Easing.OutCubic }
        }
    }
}
