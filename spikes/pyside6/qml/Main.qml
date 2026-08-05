import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

ApplicationWindow {
    id: root
    objectName: "clarifyVoiceQmlPilot"
    width: workflow.surface === "result" ? theme.resultWidth : theme.windowWidth
    height: workflow.surface === "result"
            ? theme.resultHeight
            : workflow.surface === "settings" ? theme.settingsHeight : theme.windowHeight
    minimumWidth: theme.windowWidth
    minimumHeight: theme.windowHeight
    visible: true
    title: "ClarifyVoice"
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint

    Theme { id: theme }

    // The production shell uses the 380x48 card as its idle surface and a
    // separate 142x42 transient pill while recording/processing. Keep that
    // relationship visible in the pilot without introducing a dashboard.
    StatusPill {
        id: pill
        theme: theme
        x: root.x + (root.width - width) / 2
        y: root.y + root.height + 12

        Connections {
            target: root
            function onXChanged() { pill.x = root.x + (root.width - pill.width) / 2 }
            function onYChanged() { pill.y = root.y + root.height + 12 }
            function onWidthChanged() { pill.x = root.x + (root.width - pill.width) / 2 }
            function onHeightChanged() { pill.y = root.y + root.height + 12 }
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (workflow.surface === "recording")
                workflow.cancelRecording()
            else if (workflow.surface === "result" || workflow.surface === "settings")
                workflow.reset()
        }
    }

    Rectangle {
        id: card
        objectName: "mainCard"
        anchors.fill: parent
        anchors.margins: 1
        radius: workflow.surface === "idle"
                || workflow.surface === "recording"
                || workflow.surface === "processing"
                || workflow.surface === "success"
                ? height / 2 : theme.panelRadius
        color: theme.card
        border.color: theme.border
        border.width: 1
        clip: true

        states: [
            State {
                name: "recording"
                when: workflow.surface === "recording"
                PropertyChanges { target: card; border.color: theme.dim }
            },
            State {
                name: "success"
                when: workflow.surface === "success"
                PropertyChanges { target: card; border.color: theme.text }
            }
        ]

        transitions: [
            Transition {
                from: "*"
                to: "*"
                ColorAnimation { duration: 180; easing.type: Easing.OutCubic }
            }
        ]

        StackLayout {
            id: pages
            objectName: "pilotPages"
            anchors.fill: parent
            currentIndex: workflow.surface === "result"
                          ? 1 : workflow.surface === "settings" ? 2 : 0

            Item {
                id: homePage
                objectName: "homePage"
                property bool promptMode: true

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 15
                    anchors.rightMargin: 8
                    spacing: 5

                    Item {
                        id: statusArea
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.alignment: Qt.AlignVCenter

                        // The shell is frameless, so keep the status area
                        // draggable without taking pointer ownership away
                        // from the controls to its right.
                        DragHandler {
                            id: windowDragHandler
                            target: null
                            onActiveChanged: {
                                if (active)
                                    root.startSystemMove()
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            spacing: 6

                            Label {
                                id: statusLabel
                                Layout.fillWidth: true
                                text: workflow.surface === "idle" ? "Ready"
                                      : workflow.surface === "recording" ? "Recording"
                                      : workflow.surface === "processing" ? "Processing…"
                                      : "Done"
                                color: theme.text
                                font.pixelSize: 13
                                font.weight: Font.Bold
                                elide: Text.ElideRight
                                Accessible.name: workflow.status
                            }

                            Label {
                                id: hotkeyHint
                                text: workflow.surface === "idle" ? "Alt+L" : ""
                                color: theme.dim
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            enabled: workflow.surface === "idle"
                                     || workflow.surface === "recording"
                            hoverEnabled: true
                            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                            Accessible.name: workflow.status
                            onClicked: {
                                if (workflow.surface === "recording")
                                    workflow.stopRecording()
                                else
                                    workflow.startRecording()
                            }
                        }
                    }

                    Item {
                        id: busyIndicator
                        visible: workflow.busy
                        Layout.preferredWidth: 14
                        Layout.preferredHeight: 14
                        Layout.alignment: Qt.AlignVCenter

                        Rectangle {
                            anchors.fill: parent
                            radius: width / 2
                            color: "transparent"
                            border.color: theme.dim
                            border.width: 1
                        }

                        Rectangle {
                            width: 4
                            height: 4
                            radius: 2
                            anchors.horizontalCenter: parent.horizontalCenter
                            y: 0
                            color: theme.text
                        }

                        RotationAnimation on rotation {
                            running: workflow.busy
                            from: 0
                            to: 360
                            duration: 760
                            loops: Animation.Infinite
                        }
                    }

                    RowLayout {
                        id: idleControls
                        visible: workflow.surface === "idle"
                        spacing: 4

                        PilotButton {
                            id: languageButton
                            objectName: "languageButton"
                            property string languageCode: "EN"
                            text: languageCode
                            theme: theme
                            Layout.preferredWidth: 32
                            Layout.preferredHeight: 26
                            Accessible.name: "Language: "
                                              + (languageCode === "EN"
                                                 ? "English" : "Portuguese")
                            onClicked: {
                                languageCode = languageCode === "EN" ? "PT" : "EN"
                                workflow.setLanguage(languageCode.toLowerCase())
                            }
                        }

                        PilotButton {
                            id: modeButton
                            objectName: "modeButton"
                            text: homePage.promptMode ? "Prompt" : "Transcribe"
                            theme: theme
                            Layout.preferredWidth: 78
                            Layout.preferredHeight: 26
                            Accessible.name: "Mode: "
                                              + (homePage.promptMode
                                                 ? "Prompt" : "Transcribe")
                            onClicked: {
                                homePage.promptMode = !homePage.promptMode
                                workflow.setMode(homePage.promptMode
                                                 ? "prompt" : "transcription")
                            }
                        }

                        PilotButton {
                            id: fileButton
                            objectName: "fileButton"
                            text: "Files"
                            theme: theme
                            Layout.preferredWidth: 48
                            Layout.preferredHeight: 26
                            Accessible.name: "Import audio files"
                        }

                        PilotButton {
                            id: settingsButton
                            objectName: "settingsButton"
                            text: "☰"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Open settings"
                            onClicked: workflow.openSettings()
                        }

                        PilotButton {
                            id: closeButton
                            objectName: "closeButton"
                            text: "—"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Close ClarifyVoice"
                            onClicked: root.close()
                        }
                    }

                    PilotButton {
                        id: resultButton
                        objectName: "resultButton"
                        visible: workflow.surface === "success"
                        text: "View"
                        theme: theme
                        Layout.preferredWidth: 48
                        Layout.preferredHeight: 26
                        Accessible.name: "View result"
                        onClicked: workflow.showResult()
                    }
                }
            }

            Item {
                id: resultPage
                objectName: "resultPage"
                property string copyLabel: "Copy"

                function resetCopyConfirmation() {
                    copyResetTimer.stop()
                    copyLabel = "Copy"
                }

                Timer {
                    id: copyResetTimer
                    interval: 850
                    repeat: false
                    onTriggered: resultPage.copyLabel = "Copy"
                }

                onVisibleChanged: resetCopyConfirmation()

                Connections {
                    target: workflow

                    function onSurfaceChanged() {
                        resultPage.resetCopyConfirmation()
                    }
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 5

                    RowLayout {
                        Layout.fillWidth: true

                        Label {
                            text: "Result"
                            color: theme.text
                            font.pixelSize: 13
                            font.weight: Font.Bold
                        }

                        Item { Layout.fillWidth: true }

                        PilotButton {
                            text: "—"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Dismiss result"
                            onClicked: workflow.reset()
                        }
                    }

                    Rectangle {
                        id: resultCard
                        objectName: "resultCard"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 64
                        radius: 10
                        color: theme.resultSurface
                        border.color: theme.border
                        border.width: 1

                        Label {
                            anchors.fill: parent
                            anchors.margins: 10
                            text: workflow.result
                            color: theme.secondaryText
                            font.pixelSize: 12
                            lineHeight: 1.2
                            wrapMode: Text.WordWrap
                            verticalAlignment: Text.AlignTop
                            Accessible.name: workflow.result
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 4

                        PilotButton {
                            text: resultPage.copyLabel
                            theme: theme
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Copy result"
                            onClicked: {
                                resultPage.copyLabel = "OK!"
                                copyResetTimer.restart()
                            }
                        }

                        PilotButton {
                            text: "Dismiss"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 56
                            Layout.preferredHeight: 26
                            Accessible.name: "Dismiss result"
                            onClicked: workflow.reset()
                        }

                        Item { Layout.fillWidth: true }
                    }
                }
            }

            Item {
                id: settingsPage
                objectName: "settingsPage"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true

                        Label {
                            text: "Settings"
                            color: theme.text
                            font.pixelSize: 13
                            font.weight: Font.Bold
                        }

                        Item { Layout.fillWidth: true }

                        PilotButton {
                            text: "—"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Close settings"
                            onClicked: workflow.closeSettings()
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: theme.border
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Label {
                            text: "Hotkey"
                            color: theme.text
                            font.pixelSize: 12
                        }

                        Item { Layout.fillWidth: true }

                        Label {
                            text: "Alt+L"
                            color: theme.dim
                            font.pixelSize: 11
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: "Production shell keeps global shortcuts and settings ownership."
                        color: theme.dim
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }

                    Item { Layout.fillHeight: true }

                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }

                        PilotButton {
                            text: "Close"
                            theme: theme
                            Layout.preferredWidth: 56
                            Layout.preferredHeight: 26
                            Accessible.name: "Close settings"
                            onClicked: workflow.closeSettings()
                        }
                    }
                }
            }
        }
    }
}
