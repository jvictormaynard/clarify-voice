import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

Window {
    id: pill
    objectName: "workflowStatusPill"
    width: 360
    height: 70
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
           | Qt.WindowDoesNotAcceptFocus
    visible: true
    title: "ClarifyVoice workflow status"
    property color accentColor: theme.accent
    property string label: workflow.status
    property string phase: workflow.surface

    required property Theme theme

    Rectangle {
        id: card
        anchors.fill: parent
        anchors.margins: 1
        radius: height / 2
        color: theme.surfaceRaised
        border.color: theme.border
        border.width: 1
        opacity: workflow.busy ? 1 : 0.98
        Accessible.name: "ClarifyVoice workflow status"

        Behavior on opacity {
            NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 20
            anchors.rightMargin: 20
            spacing: 12

            Rectangle {
                id: indicator
                Layout.preferredWidth: 12
                Layout.preferredHeight: 12
                radius: 6
                color: workflow.surface === "recording" ? theme.recording : pill.accentColor

                SequentialAnimation on scale {
                    running: workflow.busy
                    loops: Animation.Infinite
                    NumberAnimation { to: 1.35; duration: 560; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0; duration: 560; easing.type: Easing.InOutSine }
                }
            }

            Label {
                Layout.fillWidth: true
                text: pill.label
                color: theme.text
                font.pixelSize: 15
                font.weight: Font.Medium
                elide: Text.ElideRight
                Accessible.name: pill.label
            }

            Label {
                text: workflow.busy ? "LIVE" : "READY"
                color: workflow.busy ? theme.recording : theme.success
                font.pixelSize: 10
                font.weight: Font.DemiBold
                font.letterSpacing: 1.2
            }
        }
    }
}
