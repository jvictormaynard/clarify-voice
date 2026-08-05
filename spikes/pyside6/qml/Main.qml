import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

ApplicationWindow {
    id: root
    objectName: "clarifyVoiceQmlPilot"
    width: 760
    height: 540
    minimumWidth: 680
    minimumHeight: 480
    visible: true
    title: "ClarifyVoice · Qt Quick pilot"
    color: theme.canvas
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
           | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint
    Theme { id: theme }

    StatusPill {
        id: pill
        theme: theme
        x: root.x + (root.width - width) / 2
        y: root.y - height - 14

        Connections {
            target: root
            function onXChanged() { pill.x = root.x + (root.width - pill.width) / 2 }
            function onYChanged() { pill.y = root.y - pill.height - 14 }
            function onWidthChanged() { pill.x = root.x + (root.width - pill.width) / 2 }
        }
    }

    Shortcut {
        sequence: "Escape"
        onActivated: workflow.reset()
    }

    ColumnLayout {
        Accessible.name: "ClarifyVoice Qt Quick visual pilot"
        anchors.fill: parent
        anchors.margins: 30
        spacing: 22

        RowLayout {
            objectName: "appHeader"
            z: 2
            Layout.fillWidth: true
            spacing: 14

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3

                Label {
                    text: "ClarifyVoice"
                    color: theme.text
                    font.pixelSize: 27
                    font.weight: Font.DemiBold
                }

                Label {
                    text: "A calmer voice workflow, designed for focus"
                    color: theme.muted
                    font.pixelSize: 13
                }
            }

            Label {
                text: "QT QUICK PILOT"
                color: theme.accent
                font.pixelSize: 10
                font.weight: Font.DemiBold
                font.letterSpacing: 1.3
            }
        }

        StackLayout {
            id: pages
            objectName: "pilotPages"
            z: 1
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: workflow.surface === "result" ? 1 : workflow.surface === "settings" ? 2 : 0

            Item {
                objectName: "homePage"
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18

                    Rectangle {
                        id: workflowCard
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: theme.radiusLarge
                        color: theme.surface
                        border.color: theme.border
                        border.width: 1

                        states: [
                            State {
                                name: "recording"
                                when: workflow.surface === "recording"
                                PropertyChanges { target: workflowCard; border.color: theme.recording }
                            },
                            State {
                                name: "success"
                                when: workflow.surface === "success"
                                PropertyChanges { target: workflowCard; border.color: theme.success }
                            }
                        ]

                        transitions: [
                            Transition {
                                from: "*"
                                to: "*"
                                ColorAnimation { duration: 240; easing.type: Easing.OutCubic }
                            }
                        ]

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 28
                            spacing: 16

                            RowLayout {
                                Layout.fillWidth: true

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 6

                                    Label {
                                        text: workflow.status
                                        color: theme.text
                                        font.pixelSize: 22
                                        font.weight: Font.Medium
                                        Accessible.name: workflow.status
                                    }

                                    Label {
                                        text: workflow.surface === "idle"
                                              ? "Press record to simulate the full interaction"
                                              : "The UI state is driven by an observable workflow bridge"
                                        color: theme.muted
                                        font.pixelSize: 13
                                        wrapMode: Text.WordWrap
                                    }
                                }

                                Item {
                                    visible: workflow.surface === "processing"
                                    Layout.preferredWidth: 30
                                    Layout.preferredHeight: 30

                                    Rectangle {
                                        anchors.fill: parent
                                        radius: width / 2
                                        color: "transparent"
                                        border.color: theme.surfaceSoft
                                        border.width: 3
                                    }

                                    Item {
                                        anchors.fill: parent

                                        Rectangle {
                                            width: 8
                                            height: 8
                                            radius: 4
                                            anchors.horizontalCenter: parent.horizontalCenter
                                            y: 0
                                            color: theme.accent
                                        }

                                        RotationAnimation on rotation {
                                            from: 0
                                            to: 360
                                            duration: 760
                                            loops: Animation.Infinite
                                        }
                                    }
                                }
                            }

                            Item { Layout.fillHeight: true }

                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 92
                                radius: theme.radiusMedium
                                color: theme.surfaceRaised
                                border.color: theme.border
                                border.width: 1

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 18
                                    spacing: 14

                                    Rectangle {
                                        Layout.preferredWidth: 42
                                        Layout.preferredHeight: 42
                                        radius: 21
                                        color: workflow.busy ? theme.recording : theme.accentStrong

                                        Label {
                                            anchors.centerIn: parent
                                            text: workflow.busy ? "•••" : "✦"
                                            color: "white"
                                            font.pixelSize: 18
                                        }
                                    }

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 4

                                        Label {
                                            text: workflow.busy ? "Working on it" : "Ready when you are"
                                            color: theme.text
                                            font.pixelSize: 15
                                            font.weight: Font.Medium
                                        }

                                        Label {
                                            text: workflow.busy
                                                  ? "This pilot keeps the shell responsive while the state changes"
                                                  : "Your result will stay available for review before publication"
                                            color: theme.muted
                                            font.pixelSize: 12
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 10

                                PilotButton {
                                    objectName: "recordButton"
                                    text: workflow.busy ? "Processing…" : "Record sample"
                                    enabled: !workflow.busy && workflow.surface !== "success"
                                    primary: true
                                    theme: theme
                                    Layout.fillWidth: true
                                    Accessible.name: "Record sample"
                                    onClicked: workflow.startRecording()
                                }

                                PilotButton {
                                    objectName: "resultButton"
                                    text: "View result"
                                    enabled: workflow.canShowResult
                                    theme: theme
                                    Layout.fillWidth: true
                                    Accessible.name: "View result"
                                    onClicked: workflow.showResult()
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        Label {
                            Layout.fillWidth: true
                            text: "Fake workflow · no audio, provider, clipboard or hotkey calls"
                            color: theme.muted
                            font.pixelSize: 11
                        }

                        PilotButton {
                            text: "Settings"
                            theme: theme
                            quiet: true
                            Accessible.name: "Open prototype settings"
                            onClicked: workflow.openSettings()
                        }
                    }
                }
            }

            Item {
                objectName: "resultPage"
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 16

                    Label {
                        text: "Result"
                        color: theme.text
                        font.pixelSize: 24
                        font.weight: Font.Medium
                    }

                    Rectangle {
                        objectName: "resultCard"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: theme.radiusMedium
                        color: theme.surface
                        border.color: theme.border

                        Label {
                            anchors.fill: parent
                            anchors.margins: 22
                            text: workflow.result
                            color: theme.text
                            font.pixelSize: 16
                            lineHeight: 1.35
                            wrapMode: Text.WordWrap
                            verticalAlignment: Text.AlignTop
                            Accessible.name: workflow.result
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }
                        PilotButton {
                            text: "Back"
                            theme: theme
                            Accessible.name: "Back to workflow"
                            onClicked: workflow.reset()
                        }
                    }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 18

                    Label {
                        text: "Settings"
                        color: theme.text
                        font.pixelSize: 24
                        font.weight: Font.Medium
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: theme.radiusMedium
                        color: theme.surface
                        border.color: theme.border

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 22
                            spacing: 16

                            Label {
                                text: "A focused, calmer control surface"
                                color: theme.text
                                font.pixelSize: 16
                                font.weight: Font.Medium
                            }

                            Label {
                                text: "The production settings controller will be connected only after this visual pilot passes its interaction review."
                                color: theme.muted
                                font.pixelSize: 13
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 1
                                color: theme.border
                            }

                            Label {
                                text: "Hotkey ownership"
                                color: theme.text
                                font.pixelSize: 13
                            }

                            Label {
                                text: "Production shell remains responsible for global shortcuts"
                                color: theme.muted
                                font.pixelSize: 12
                            }

                            Item { Layout.fillHeight: true }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }
                        PilotButton {
                            text: "Close settings"
                            theme: theme
                            Accessible.name: "Close prototype settings"
                            onClicked: workflow.closeSettings()
                        }
                    }
                }
            }
        }
    }
}
