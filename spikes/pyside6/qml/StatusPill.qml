import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

Window {
    id: pill
    objectName: "workflowStatusPill"
    width: 142
    height: 42
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
           | Qt.WindowDoesNotAcceptFocus
    visible: workflow.busy || workflow.surface === "success"
    title: "ClarifyVoice workflow status"

    required property Theme theme
    property string label: workflow.status

    Rectangle {
        id: card
        anchors.fill: parent
        anchors.margins: 1
        radius: height / 2
        color: theme.card
        border.color: theme.border
        border.width: 1
        Accessible.name: workflow.status

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            spacing: 8

            Rectangle {
                id: indicator
                Layout.preferredWidth: 8
                Layout.preferredHeight: 8
                radius: 4
                color: theme.text
                opacity: workflow.surface === "success" ? 1.0 : 0.72

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
                font.pixelSize: 11
                font.weight: Font.Normal
                elide: Text.ElideRight
                Accessible.name: workflow.status
            }

            Label {
                text: workflow.busy ? "LIVE" : "OK"
                color: theme.dim
                font.pixelSize: 9
                font.weight: Font.DemiBold
                font.letterSpacing: 0.8
            }
        }
    }
}
