import QtQuick 6.5
import QtQuick.Controls 6.5

Button {
    id: control
    required property Theme theme
    property bool primary: false
    property bool quiet: false

    implicitHeight: control.theme.controlHeight
    hoverEnabled: true

    contentItem: Label {
        text: control.text
        color: !control.enabled
               ? control.theme.dim
               : control.quiet ? control.theme.dim : control.theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        font.pixelSize: 11
        font.weight: Font.Normal
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: control.theme.controlRadius
        color: !control.enabled
               ? control.theme.controlDisabled
               : control.quiet
                 ? (control.hovered ? control.theme.controlHover : "transparent")
                 : (control.hovered ? control.theme.controlHover : control.theme.control)
        border.width: 0

        Behavior on color {
            ColorAnimation { duration: 110; easing.type: Easing.OutCubic }
        }
    }
}
