import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Dialogs 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

ApplicationWindow {
    id: root
    objectName: "clarifyVoiceMainWindow"
    width: workflow.surface === "result"
           || workflow.surface === "voice_result"
           || workflow.surface === "voice_error" ? theme.resultWidth
           : (workflow.surface === "settings"
              || workflow.surface === "files"
              || workflow.surface === "translation_picker")
             ? theme.panelWidth : theme.windowWidth
    height: workflow.surface === "result"
            || workflow.surface === "voice_result"
            || workflow.surface === "voice_error"
            ? theme.resultHeight
            : (workflow.surface === "settings"
               || workflow.surface === "files"
               || workflow.surface === "translation_picker")
              ? theme.panelHeight : theme.windowHeight
    minimumWidth: theme.windowWidth
    minimumHeight: theme.windowHeight
    visible: true
    title: "ClarifyVoice"
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint

    Theme { id: theme }
    property Theme visualTheme: theme

    // The production shell uses the 380x48 card as its idle surface and a
    // separate 142x42 transient pill while recording/processing. Keep that
    // relationship visible in the production shell without introducing a dashboard.
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
            else if (workflow.surface === "translation_picker")
                workflow.cancelTranslation()
            else if (workflow.surface === "result" || workflow.surface === "settings")
                workflow.reset()
            else if (workflow.surface === "error")
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
                || workflow.surface === "voice_processing"
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
            objectName: "appPages"
            anchors.fill: parent
            currentIndex: workflow.surface === "result"
                          ? 1
                          : workflow.surface === "settings" ? 2
                          : workflow.surface === "files" ? 3
                          : workflow.surface === "translation_picker" ? 4
                          : (workflow.surface === "voice_result"
                             || workflow.surface === "voice_error") ? 1 : 0

            Item {
                id: homePage
                objectName: "homePage"
                property bool promptMode: workflow.mode === "prompt"

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
                                      : workflow.surface === "voice_processing" ? workflow.status
                                      : workflow.surface === "error" ? workflow.status
                                      : "Done"
                                color: theme.text
                                font.pixelSize: 13
                                font.weight: Font.Bold
                                elide: Text.ElideRight
                                Accessible.name: workflow.status
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

                        AppButton {
                            id: languageButton
                            objectName: "languageButton"
                            readonly property var supportedLanguages: [
                                "en", "pt", "es", "de", "ru"
                            ]
                            readonly property var languageNames: ({
                                "en": "English",
                                "pt": "Portuguese",
                                "es": "Spanish",
                                "de": "German",
                                "ru": "Russian"
                            })
                            property string languageCode: workflow.language.toUpperCase()
                            text: languageCode
                            theme: theme
                            Layout.preferredWidth: 32
                            Layout.preferredHeight: 26
                            Accessible.name: "Language: "
                                              + languageNames[workflow.language]
                            onClicked: {
                                var currentIndex = supportedLanguages.indexOf(workflow.language)
                                var nextIndex = (currentIndex + 1) % supportedLanguages.length
                                workflow.setLanguage(supportedLanguages[nextIndex])
                            }
                        }

                        AppButton {
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
                                workflow.setMode(homePage.promptMode
                                                 ? "transcription" : "prompt")
                            }
                        }

                        AppButton {
                            id: fileButton
                            objectName: "fileButton"
                            text: "Files"
                            theme: theme
                            Layout.preferredWidth: 48
                            Layout.preferredHeight: 26
                            Accessible.name: "Import audio files"
                            onClicked: workflow.openFiles()
                        }

                        AppButton {
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

                        AppButton {
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

                    RowLayout {
                        id: errorControls
                        visible: workflow.surface === "error"
                        spacing: 4

                        AppButton {
                            text: "Dismiss"
                            theme: theme
                            Layout.preferredWidth: 60
                            Layout.preferredHeight: 26
                            Accessible.name: "Dismiss workflow error"
                            onClicked: workflow.reset()
                        }

                        Item { Layout.fillWidth: true }
                    }

                    AppButton {
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

                    function onCopyCompleted(success) {
                        if (success) {
                            resultPage.copyLabel = "OK!"
                            copyResetTimer.restart()
                        } else {
                            resultPage.resetCopyConfirmation()
                        }
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

                        AppButton {
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

                        AppButton {
                            text: resultPage.copyLabel
                            theme: theme
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Copy result"
                            onClicked: workflow.copyResult()
                        }

                        AppButton {
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
                    anchors.margins: 10
                    spacing: 6

                    RowLayout {
                        Layout.fillWidth: true

                        Label {
                            text: "Settings"
                            color: theme.text
                            font.pixelSize: 13
                            font.weight: Font.Bold
                        }

                        Item { Layout.fillWidth: true }

                        AppButton {
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

                    ScrollView {
                        id: settingsScroll
                        objectName: "settingsScroll"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true

                        ScrollBar.vertical: ScrollBar {
                            policy: ScrollBar.AsNeeded
                            contentItem: Rectangle {
                                implicitWidth: 4
                                radius: 2
                                color: theme.dim
                            }
                            background: Rectangle {
                                implicitWidth: 4
                                color: "transparent"
                            }
                        }

                        ColumnLayout {
                            id: settingsContent
                            width: Math.max(0, settingsScroll.availableWidth)
                            spacing: 8

                            Label {
                                text: "General"
                                color: theme.secondaryText
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                font.letterSpacing: 0.7
                            }

                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 12
                                rowSpacing: 6

                                Label {
                                    text: "Mode"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                ComboBox {
                                    id: settingsModeBox
                                    objectName: "settingsModeBox"
                                    Layout.fillWidth: true
                                    model: settings.modes
                                    currentIndex: Math.max(0, settings.modes.indexOf(settings.mode))
                                    onActivated: settings.setMode(currentText)
                                    contentItem: Label {
                                        leftPadding: 8
                                        rightPadding: 24
                                        text: settingsModeBox.currentText
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideRight
                                    }
                                    indicator: Label {
                                        x: settingsModeBox.width - width - 8
                                        y: (settingsModeBox.height - height) / 2
                                        text: "⌄"
                                        color: theme.dim
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Language"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                ComboBox {
                                    id: settingsLanguageBox
                                    objectName: "settingsLanguageBox"
                                    Layout.fillWidth: true
                                    model: settings.languages
                                    currentIndex: Math.max(0, settings.languages.indexOf(settings.language))
                                    onActivated: settings.setLanguage(currentText)
                                    contentItem: Label {
                                        leftPadding: 8
                                        rightPadding: 24
                                        text: settingsLanguageBox.currentText.toUpperCase()
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: Label {
                                        x: settingsLanguageBox.width - width - 8
                                        y: (settingsLanguageBox.height - height) / 2
                                        text: "⌄"
                                        color: theme.dim
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Start with Windows"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                CheckBox {
                                    id: autostartBox
                                    objectName: "autostartBox"
                                    Layout.fillWidth: true
                                    checked: settings.autostart
                                    text: "Autostart"
                                    onToggled: settings.setAutostart(checked)
                                    contentItem: Label {
                                        leftPadding: 24
                                        text: autostartBox.text
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: Rectangle {
                                        x: 0
                                        y: (autostartBox.height - height) / 2
                                        implicitWidth: 16
                                        implicitHeight: 16
                                        radius: 3
                                        color: autostartBox.checked ? theme.text : theme.control
                                        border.color: autostartBox.checked ? theme.text : theme.border
                                        border.width: 1

                                        Label {
                                            anchors.centerIn: parent
                                            visible: autostartBox.checked
                                            text: "✓"
                                            color: theme.card
                                            font.pixelSize: 11
                                        }
                                    }
                                }

                                Label {
                                    text: "History"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8

                                    CheckBox {
                                        id: historyBox
                                        objectName: "historyBox"
                                        checked: settings.historyEnabled
                                        text: "Keep local history"
                                        onToggled: settings.setHistoryEnabled(checked)
                                        contentItem: Label {
                                            leftPadding: 24
                                            text: historyBox.text
                                            color: theme.text
                                            font.pixelSize: 11
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                        indicator: Rectangle {
                                            x: 0
                                            y: (historyBox.height - height) / 2
                                            implicitWidth: 16
                                            implicitHeight: 16
                                            radius: 3
                                            color: historyBox.checked ? theme.text : theme.control
                                            border.color: historyBox.checked ? theme.text : theme.border
                                            border.width: 1

                                            Label {
                                                anchors.centerIn: parent
                                                visible: historyBox.checked
                                                text: "✓"
                                                color: theme.card
                                                font.pixelSize: 11
                                            }
                                        }
                                    }

                                    Label {
                                        text: "days"
                                        color: theme.dim
                                        font.pixelSize: 11
                                    }

                                    TextField {
                                        id: historyRetentionField
                                        objectName: "historyRetentionField"
                                        Layout.preferredWidth: 62
                                        enabled: historyBox.checked
                                        text: settings.historyRetentionDays === null
                                              || settings.historyRetentionDays === undefined
                                              ? "" : String(settings.historyRetentionDays)
                                        inputMethodHints: Qt.ImhDigitsOnly
                                        validator: IntValidator { bottom: 0; top: 3650 }
                                        onEditingFinished: settings.setHistoryRetentionDays(
                                            text.trim() === "" ? null : Number(text)
                                        )
                                        color: theme.text
                                        font.pixelSize: 11
                                        selectByMouse: true
                                        background: Rectangle {
                                            implicitHeight: 26
                                            radius: theme.controlRadius
                                            color: theme.control
                                            border.color: theme.border
                                            border.width: 1
                                        }
                                    }
                                }
                            }

                            Label {
                                text: "Microphone and recording"
                                color: theme.secondaryText
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                font.letterSpacing: 0.7
                            }

                            Label {
                                Layout.fillWidth: true
                                text: "Choose a stable input endpoint. A missing endpoint can be recovered with System default."
                                color: theme.dim
                                font.pixelSize: 10
                                wrapMode: Text.WordWrap
                            }

                            function recordingControlsDraft() {
                                var current = settings.recordingControls
                                var vad = current["vad"] || {}
                                return {
                                    "max_duration_seconds": maximumDurationField.text.trim() === ""
                                                               ? null : Number(maximumDurationField.text),
                                    "warning_seconds": Number(warningSecondsField.text),
                                    "vad": {
                                        "enabled": vadEnabledBox.checked,
                                        "level_threshold": Number(vadLevelField.text),
                                        "minimum_speech_seconds": Number(minimumSpeechField.text),
                                        "silence_duration_seconds": Number(silenceDurationField.text)
                                    }
                                }
                            }

                            function updateRecordingControls() {
                                settings.setRecordingControls(recordingControlsDraft())
                            }

                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 12
                                rowSpacing: 6

                                Label {
                                    text: "Input"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 5

                                    ComboBox {
                                        id: microphoneBox
                                        objectName: "microphoneBox"
                                        Layout.fillWidth: true
                                        model: settings.microphoneDevices
                                        textRole: "label"
                                        currentIndex: Math.max(
                                            0,
                                            Math.min(settings.microphoneSelectionIndex, count - 1))
                                        onActivated: settings.selectMicrophone(
                                            microphoneBox.model[index]["id"])
                                        contentItem: Label {
                                            leftPadding: 8
                                            rightPadding: 24
                                            text: microphoneBox.currentText
                                            color: theme.text
                                            font.pixelSize: 11
                                            verticalAlignment: Text.AlignVCenter
                                            elide: Text.ElideRight
                                        }
                                        indicator: Label {
                                            x: microphoneBox.width - width - 8
                                            y: (microphoneBox.height - height) / 2
                                            text: "⌄"
                                            color: theme.dim
                                            font.pixelSize: 12
                                        }
                                        background: Rectangle {
                                            implicitHeight: 26
                                            radius: theme.controlRadius
                                            color: theme.control
                                            border.color: theme.border
                                            border.width: 1
                                        }
                                    }

                                    AppButton {
                                        text: "↻"
                                        theme: theme
                                        quiet: true
                                        Layout.preferredWidth: 28
                                        Layout.preferredHeight: 26
                                        Accessible.name: "Refresh microphone inventory"
                                        enabled: !settings.microphoneTestBusy
                                        onClicked: settings.refreshMicrophoneInventory()
                                    }
                                }

                                Label {
                                    text: "Input status"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 5

                                    Label {
                                        Layout.fillWidth: true
                                        text: settings.microphoneStatus
                                        color: settings.microphoneStatusKind === "error"
                                               ? theme.secondaryText
                                               : settings.microphoneStatusKind === "warning"
                                                 ? "#d3a46f" : theme.dim
                                        font.pixelSize: 10
                                        wrapMode: Text.WordWrap
                                    }

                                    AppButton {
                                        text: "Use default"
                                        theme: theme
                                        quiet: true
                                        visible: settings.selectedMicrophoneId !== null
                                        Layout.preferredWidth: 78
                                        Layout.preferredHeight: 26
                                        Accessible.name: "Use current system microphone"
                                        onClicked: settings.selectMicrophone("")
                                    }

                                    AppButton {
                                        text: settings.microphoneTestBusy ? "Listening…" : "Test"
                                        theme: theme
                                        enabled: !settings.microphoneTestBusy
                                        Layout.preferredWidth: 62
                                        Layout.preferredHeight: 26
                                        Accessible.name: "Test microphone input"
                                        onClicked: settings.testMicrophone()
                                    }
                                }

                                Label {
                                    text: "Maximum seconds"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: maximumDurationField
                                    objectName: "maximumDurationField"
                                    Layout.fillWidth: true
                                    text: settings.recordingControls["max_duration_seconds"] === null
                                          || settings.recordingControls["max_duration_seconds"] === undefined
                                          ? "" : String(settings.recordingControls["max_duration_seconds"])
                                    placeholderText: "Blank = unlimited"
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: settingsContent.updateRecordingControls()
                                    color: theme.text
                                    placeholderTextColor: theme.dim
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Warning seconds"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: warningSecondsField
                                    objectName: "warningSecondsField"
                                    Layout.fillWidth: true
                                    text: String(settings.recordingControls["warning_seconds"])
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: settingsContent.updateRecordingControls()
                                    color: theme.text
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                CheckBox {
                                    id: vadEnabledBox
                                    objectName: "vadEnabledBox"
                                    Layout.columnSpan: 2
                                    checked: settings.recordingControls["vad"]["enabled"]
                                    text: "Stop after speech and silence"
                                    onToggled: settingsContent.updateRecordingControls()
                                    contentItem: Label {
                                        leftPadding: 24
                                        text: vadEnabledBox.text
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: Rectangle {
                                        x: 0
                                        y: (vadEnabledBox.height - height) / 2
                                        implicitWidth: 16
                                        implicitHeight: 16
                                        radius: 3
                                        color: vadEnabledBox.checked ? theme.text : theme.control
                                        border.color: vadEnabledBox.checked ? theme.text : theme.border
                                        border.width: 1

                                        Label {
                                            anchors.centerIn: parent
                                            visible: vadEnabledBox.checked
                                            text: "✓"
                                            color: theme.card
                                            font.pixelSize: 11
                                        }
                                    }
                                }

                                Label {
                                    text: "Level threshold"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: vadLevelField
                                    objectName: "vadLevelField"
                                    Layout.fillWidth: true
                                    text: String(settings.recordingControls["vad"]["level_threshold"])
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: settingsContent.updateRecordingControls()
                                    color: theme.text
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Minimum speech seconds"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: minimumSpeechField
                                    objectName: "minimumSpeechField"
                                    Layout.fillWidth: true
                                    text: String(settings.recordingControls["vad"]["minimum_speech_seconds"])
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: settingsContent.updateRecordingControls()
                                    color: theme.text
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Silence seconds"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: silenceDurationField
                                    objectName: "silenceDurationField"
                                    Layout.fillWidth: true
                                    text: String(settings.recordingControls["vad"]["silence_duration_seconds"])
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: settingsContent.updateRecordingControls()
                                    color: theme.text
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: settings.microphoneTestStatus !== ""
                                text: settings.microphoneTestStatus
                                color: settings.microphoneTestStatusKind === "error"
                                       ? theme.secondaryText
                                       : settings.microphoneTestStatusKind === "ok"
                                         ? "#69c58a" : theme.dim
                                font.pixelSize: 10
                                wrapMode: Text.WordWrap
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 1
                                color: theme.border
                            }

                            Label {
                                text: "Providers"
                                color: theme.secondaryText
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                font.letterSpacing: 0.7
                            }

                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 12
                                rowSpacing: 6

                                Label {
                                    text: "Provider"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                ComboBox {
                                    id: onboardingProviderBox
                                    objectName: "onboardingProviderBox"
                                    Layout.fillWidth: true
                                    model: settings.providerIds
                                    currentIndex: Math.max(
                                        0, settings.providerIds.indexOf(
                                            settings.selectedProviderId))
                                    onActivated: settings.selectProvider(currentText)
                                    contentItem: Label {
                                        leftPadding: 8
                                        rightPadding: 24
                                        text: settings.providerName(
                                            onboardingProviderBox.currentText)
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: Label {
                                        x: onboardingProviderBox.width - width - 8
                                        y: (onboardingProviderBox.height - height) / 2
                                        text: "⌄"
                                        color: theme.dim
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "API key"
                                    visible: settings.selectedProviderId !== "local_asr"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: providerApiKeyField
                                    objectName: "providerApiKeyField"
                                    visible: settings.selectedProviderId !== "local_asr"
                                    Layout.fillWidth: true
                                    text: settings.providerApiKey
                                    placeholderText: settings.providerHasApiKey
                                                     ? "Saved key; leave blank to keep it"
                                                     : "Paste API key"
                                    echoMode: TextInput.Password
                                    onEditingFinished: settings.setProviderApiKey(text)
                                    color: theme.text
                                    placeholderTextColor: theme.dim
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Base URL"
                                    visible: settings.selectedProviderId !== "local_asr"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: providerBaseUrlField
                                    objectName: "providerBaseUrlField"
                                    visible: settings.selectedProviderId !== "local_asr"
                                    enabled: settings.providerSupportsCustomEndpoint
                                    Layout.fillWidth: true
                                    text: settings.providerBaseUrl
                                    onEditingFinished: settings.setProviderBaseUrl(text)
                                    color: theme.text
                                    placeholderText: "Provider endpoint"
                                    placeholderTextColor: theme.dim
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: settings.selectedProviderId !== "local_asr"
                                text: "Status: " + settings.providerStatus
                                      + (settings.providerError !== ""
                                         ? " — " + settings.providerError : "")
                                color: settings.providerError !== ""
                                       ? theme.secondaryText : theme.dim
                                font.pixelSize: 10
                                wrapMode: Text.WordWrap
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                visible: settings.selectedProviderId !== "local_asr"
                                spacing: 5

                                Item { Layout.fillWidth: true }

                                AppButton {
                                    text: settings.providerBusy ? "Validating…"
                                          : "Validate & save"
                                    theme: theme
                                    enabled: !settings.providerBusy
                                    Layout.preferredWidth: 108
                                    Layout.preferredHeight: 26
                                    Accessible.name: "Validate and save provider"
                                    onClicked: settings.validateProvider()
                                }

                                AppButton {
                                    text: "Clear key"
                                    theme: theme
                                    quiet: true
                                    enabled: settings.providerHasApiKey
                                    Layout.preferredWidth: 68
                                    Layout.preferredHeight: 26
                                    Accessible.name: "Clear provider API key"
                                    onClicked: settings.clearProvider()
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                visible: settings.selectedProviderId === "local_asr"
                                spacing: 5

                                Label {
                                    Layout.fillWidth: true
                                    text: settings.localAsrRequirements
                                    color: theme.dim
                                    font.pixelSize: 10
                                    wrapMode: Text.WordWrap
                                }

                                ProgressBar {
                                    Layout.fillWidth: true
                                    visible: settings.localAsrBusy
                                    value: settings.localAsrProgress
                                    from: 0
                                    to: 1
                                }

                                Label {
                                    Layout.fillWidth: true
                                    text: settings.localAsrStatus
                                          + (settings.localAsrDetail !== ""
                                             ? " — " + settings.localAsrDetail : "")
                                    color: settings.localAsrStatus === "error"
                                           || settings.localAsrStatus === "invalid"
                                           ? theme.secondaryText : theme.dim
                                    font.pixelSize: 10
                                    wrapMode: Text.WordWrap
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 5

                                    AppButton {
                                        text: settings.localAsrBusy ? "Cancel"
                                              : settings.localAsrStatus === "installed"
                                                ? "Installed" : "Download local ASR"
                                        theme: theme
                                        enabled: settings.localAsrBusy
                                                  || settings.localAsrStatus !== "installed"
                                        Layout.preferredWidth: 126
                                        Layout.preferredHeight: 26
                                        Accessible.name: "Install Local Whisper"
                                        onClicked: settings.localAsrBusy
                                                   ? settings.cancelLocalAsr()
                                                   : settings.installLocalAsr()
                                    }

                                    AppButton {
                                        text: "Remove assets"
                                        theme: theme
                                        quiet: true
                                        enabled: !settings.localAsrBusy
                                                  && settings.localAsrStatus === "installed"
                                        Layout.preferredWidth: 86
                                        Layout.preferredHeight: 26
                                        Accessible.name: "Remove Local Whisper assets"
                                        onClicked: settings.removeLocalAsr()
                                    }

                                    CheckBox {
                                        id: localRefinementBox
                                        visible: settings.localAsrStatus === "installed"
                                        checked: settings.localAsrCloudRefinement
                                        text: "Allow cloud refinement"
                                        onToggled: settings.setLocalAsrCloudRefinement(checked)
                                        contentItem: Label {
                                            leftPadding: 24
                                            text: localRefinementBox.text
                                            color: theme.dim
                                            font.pixelSize: 10
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                    }
                                }
                            }

                            Label {
                                text: "Workflow route"
                                color: theme.secondaryText
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                font.letterSpacing: 0.7
                            }

                            function scopeLabel(scope) {
                                var labels = {
                                    "transcription": "Transcription",
                                    "refinement": "Refinement",
                                    "rewrite": "Rewrite",
                                    "translation": "Translation",
                                    "local_asr_refinement": "Local ASR refinement"
                                }
                                return labels[scope] || scope
                            }

                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 12
                                rowSpacing: 6

                                Label {
                                    text: "Scope"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                ComboBox {
                                    id: scopeBox
                                    objectName: "workflowScopeBox"
                                    Layout.fillWidth: true
                                    model: settings.workflowScopes
                                    currentIndex: Math.max(0, settings.workflowScopes.indexOf(settings.selectedScope))
                                    onActivated: settings.selectWorkflow(currentText)
                                    contentItem: Label {
                                        leftPadding: 8
                                        rightPadding: 24
                                        text: settingsContent.scopeLabel(scopeBox.currentText)
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideRight
                                    }
                                    indicator: Label {
                                        x: scopeBox.width - width - 8
                                        y: (scopeBox.height - height) / 2
                                        text: "⌄"
                                        color: theme.dim
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Provider"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                ComboBox {
                                    id: providerBox
                                    objectName: "workflowProviderBox"
                                    Layout.fillWidth: true
                                    model: settings.providersForScope(settings.selectedScope)
                                    currentIndex: Math.max(0, model.indexOf(settings.routeProviderId))
                                    onActivated: settings.setRouteProviderId(currentText)
                                    contentItem: Label {
                                        leftPadding: 8
                                        rightPadding: 24
                                        text: providerBox.currentText || "Select provider"
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideRight
                                    }
                                    indicator: Label {
                                        x: providerBox.width - width - 8
                                        y: (providerBox.height - height) / 2
                                        text: "⌄"
                                        color: theme.dim
                                        font.pixelSize: 12
                                    }
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Model"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: routeModelField
                                    objectName: "workflowModelField"
                                    Layout.fillWidth: true
                                    text: settings.routeModelId
                                    onEditingFinished: settings.setRouteModelId(text)
                                    color: theme.text
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Endpoint"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                TextField {
                                    id: routeEndpointField
                                    objectName: "workflowEndpointField"
                                    Layout.fillWidth: true
                                    text: settings.routeCustomEndpoint
                                    placeholderText: "Optional custom endpoint"
                                    onEditingFinished: settings.setRouteCustomEndpoint(text)
                                    color: theme.text
                                    placeholderTextColor: theme.dim
                                    font.pixelSize: 11
                                    selectByMouse: true
                                    background: Rectangle {
                                        implicitHeight: 26
                                        radius: theme.controlRadius
                                        color: theme.control
                                        border.color: theme.border
                                        border.width: 1
                                    }
                                }

                                Label {
                                    text: "Enabled"
                                    color: theme.dim
                                    font.pixelSize: 11
                                }

                                CheckBox {
                                    id: routeEnabledBox
                                    objectName: "workflowEnabledBox"
                                    Layout.fillWidth: true
                                    checked: settings.routeEnabled
                                    text: "Use this route"
                                    onToggled: settings.setRouteEnabled(checked)
                                    contentItem: Label {
                                        leftPadding: 24
                                        text: routeEnabledBox.text
                                        color: theme.text
                                        font.pixelSize: 11
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: Rectangle {
                                        x: 0
                                        y: (routeEnabledBox.height - height) / 2
                                        implicitWidth: 16
                                        implicitHeight: 16
                                        radius: 3
                                        color: routeEnabledBox.checked ? theme.text : theme.control
                                        border.color: routeEnabledBox.checked ? theme.text : theme.border
                                        border.width: 1

                                        Label {
                                            anchors.centerIn: parent
                                            visible: routeEnabledBox.checked
                                            text: "✓"
                                            color: theme.card
                                            font.pixelSize: 11
                                        }
                                    }
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                text: "Prompt"
                                color: theme.dim
                                font.pixelSize: 11
                            }

                            TextArea {
                                id: routePromptField
                                objectName: "workflowPromptField"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 52
                                text: settings.routePrompt
                                placeholderText: "Optional route instruction"
                                onEditingFinished: settings.setRoutePrompt(text)
                                color: theme.text
                                placeholderTextColor: theme.dim
                                font.pixelSize: 11
                                wrapMode: TextArea.Wrap
                                selectByMouse: true
                                background: Rectangle {
                                    implicitHeight: 52
                                    radius: theme.controlRadius
                                    color: theme.control
                                    border.color: theme.border
                                    border.width: 1
                                }
                            }

                            Label {
                                id: settingsErrorLabel
                                objectName: "settingsErrorLabel"
                                Layout.fillWidth: true
                                visible: settings.lastError !== ""
                                text: settings.lastError
                                color: theme.secondaryText
                                font.pixelSize: 10
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 5

                        Label {
                            objectName: "settingsDirtyLabel"
                            text: settings.dirty ? "Unsaved changes" : "Saved"
                            color: settings.dirty ? theme.secondaryText : theme.dim
                            font.pixelSize: 10
                        }

                        Item { Layout.fillWidth: true }

                        AppButton {
                            text: "Load"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Reload settings"
                            onClicked: settings.load()
                        }

                        AppButton {
                            text: "Save"
                            theme: theme
                            enabled: settings.dirty
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Save settings"
                            onClicked: settings.save()
                        }

                        AppButton {
                            text: "Close"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 56
                            Layout.preferredHeight: 26
                            Accessible.name: "Close settings"
                            onClicked: workflow.closeSettings()
                        }
                    }
                }
            }

            Item {
                id: filesPage
                objectName: "filesPage"

                // File imports are an explicit batch route.  Start with the
                // saved transcription route, but keep every picker change
                // local to this batch instead of mutating Settings.
                property var persistedTranscriptionRoute:
                    settings.routes["transcription"]
                property string batchExecution:
                    persistedTranscriptionRoute.providerId === "local_asr"
                    ? "local" : "cloud"
                property string batchProviderId:
                    persistedTranscriptionRoute.providerId || ""
                property string batchModelId:
                    persistedTranscriptionRoute.modelId || ""
                property bool batchRouteTouched: false
                readonly property var defaultAudioModels: ({
                    "gemini": "gemini-2.5-flash",
                    "openai": "whisper-1",
                    "groq": "whisper-large-v3-turbo",
                    "local_asr": "ggml-small"
                })

                function providerIdsForExecution(execution) {
                    var providers = settings.providersForScope("transcription")
                    var selected = []
                    for (var index = 0; index < providers.length; ++index) {
                        var providerId = providers[index]
                        if ((execution === "local" && providerId === "local_asr")
                                || (execution === "cloud" && providerId !== "local_asr"))
                            selected.push(providerId)
                    }
                    return selected
                }

                function modelIdsForProvider(providerId) {
                    var models = []
                    var addModel = function(model) {
                        var value = String(model || "").trim()
                        if (value !== "" && models.indexOf(value) < 0)
                            models.push(value)
                    }
                    var route = persistedTranscriptionRoute
                    if (route && route.providerId === providerId)
                        addModel(route.modelId)
                    var state = settings.providerStates[providerId]
                    if (state && state.models) {
                        for (var index = 0; index < state.models.length; ++index)
                            addModel(state.models[index])
                    }
                    addModel(defaultAudioModels[providerId])
                    return models
                }

                function defaultModelForProvider(providerId) {
                    var route = persistedTranscriptionRoute
                    if (route && route.providerId === providerId && route.modelId)
                        return route.modelId
                    var models = modelIdsForProvider(providerId)
                    return models.length > 0 ? models[0] : ""
                }

                function initializeBatchRoute() {
                    var route = persistedTranscriptionRoute
                    var providerId = route && route.providerId
                            ? route.providerId : ""
                    var execution = providerId === "local_asr" ? "local" : "cloud"
                    var providers = providerIdsForExecution(execution)
                    if (providers.indexOf(providerId) < 0)
                        providerId = providers.length > 0 ? providers[0] : ""
                    batchExecution = execution
                    batchProviderId = providerId
                    batchModelId = route && route.providerId === providerId
                            ? route.modelId : defaultModelForProvider(providerId)
                    batchRouteTouched = false
                }

                function selectBatchExecution(execution) {
                    var providers = providerIdsForExecution(execution)
                    var providerId = providers.indexOf(batchProviderId) >= 0
                            ? batchProviderId
                            : (providers.length > 0 ? providers[0] : "")
                    batchExecution = execution
                    batchProviderId = providerId
                    batchModelId = defaultModelForProvider(providerId)
                    batchRouteTouched = true
                }

                function selectBatchProvider(providerId) {
                    if (providerIdsForExecution(batchExecution).indexOf(providerId) < 0)
                        return
                    batchProviderId = providerId
                    batchModelId = defaultModelForProvider(providerId)
                    batchRouteTouched = true
                }

                function selectBatchModel(model) {
                    batchModelId = String(model || "").trim()
                    batchRouteTouched = true
                }

                function commitBatchModel() {
                    var value = batchModelId
                    if (batchModelBox.editable)
                        value = batchModelBox.editText
                    batchModelId = String(value || "").trim()
                    batchRouteTouched = true
                }

                function startBatch() {
                    commitBatchModel()
                    audioBatch.start(
                        audioBatch.selectedFiles,
                        batchProviderId,
                        batchModelId,
                        workflow.language,
                        workflow.mode
                    )
                }

                function retryBatch() {
                    commitBatchModel()
                    audioBatch.retryFailed(
                        batchProviderId,
                        batchModelId,
                        workflow.language,
                        workflow.mode
                    )
                }

                Component.onCompleted: initializeBatchRoute()

                Connections {
                    target: settings

                    function onConfigChanged() {
                        if (!filesPage.batchRouteTouched && !audioBatch.running)
                            filesPage.initializeBatchRoute()
                    }
                }

                FileDialog {
                    id: audioFileDialog
                    title: "Import audio files"
                    fileMode: FileDialog.OpenFiles
                    nameFilters: [
                        "Audio files (*.wav *.aif *.aiff *.au *.flac *.oga *.ogg *.wv)",
                        "All files (*)"
                    ]
                    onAccepted: {
                        var paths = []
                        for (var index = 0; index < selectedFiles.length; ++index)
                            paths.push(selectedFiles[index].toLocalFile())
                        audioBatch.setSelectedFiles(paths)
                    }
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 6

                    RowLayout {
                        Layout.fillWidth: true

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Label {
                                text: "Import audio"
                                color: theme.text
                                font.pixelSize: 13
                                font.weight: Font.Bold
                            }

                            Label {
                                text: audioBatch.running
                                      ? "Transcribing selected files"
                                      : "Select local files to transcribe"
                                color: theme.dim
                                font.pixelSize: 10
                            }
                        }

                        AppButton {
                            text: "—"
                            theme: theme
                            quiet: true
                            enabled: !audioBatch.running
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Close audio import"
                            onClicked: workflow.closeFiles()
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: theme.border
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 4
                        columnSpacing: 7
                        rowSpacing: 5

                        Label {
                            text: "Route"
                            color: theme.dim
                            font.pixelSize: 10
                        }

                        ComboBox {
                            id: batchExecutionBox
                            objectName: "batchExecutionBox"
                            enabled: !audioBatch.running
                            Layout.fillWidth: true
                            model: ["local", "cloud"]
                            currentIndex: Math.max(
                                0, model.indexOf(filesPage.batchExecution))
                            onActivated: filesPage.selectBatchExecution(currentText)
                            contentItem: Label {
                                leftPadding: 8
                                rightPadding: 24
                                text: batchExecutionBox.currentText === "local"
                                      ? "Local Whisper" : "Cloud"
                                color: theme.text
                                font.pixelSize: 10
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            indicator: Label {
                                x: batchExecutionBox.width - width - 8
                                y: (batchExecutionBox.height - height) / 2
                                text: "⌄"
                                color: theme.dim
                                font.pixelSize: 12
                            }
                            background: Rectangle {
                                implicitHeight: 26
                                radius: theme.controlRadius
                                color: theme.control
                                border.color: theme.border
                                border.width: 1
                            }
                        }

                        Label {
                            text: "Provider"
                            color: theme.dim
                            font.pixelSize: 10
                        }

                        ComboBox {
                            id: batchProviderBox
                            objectName: "batchProviderBox"
                            enabled: !audioBatch.running
                            Layout.fillWidth: true
                            model: filesPage.providerIdsForExecution(
                                filesPage.batchExecution)
                            currentIndex: Math.max(
                                0, model.indexOf(filesPage.batchProviderId))
                            onActivated: filesPage.selectBatchProvider(currentText)
                            contentItem: Label {
                                leftPadding: 8
                                rightPadding: 24
                                text: settings.providerName(
                                    batchProviderBox.currentText)
                                color: theme.text
                                font.pixelSize: 10
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideRight
                            }
                            indicator: Label {
                                x: batchProviderBox.width - width - 8
                                y: (batchProviderBox.height - height) / 2
                                text: "⌄"
                                color: theme.dim
                                font.pixelSize: 12
                            }
                            background: Rectangle {
                                implicitHeight: 26
                                radius: theme.controlRadius
                                color: theme.control
                                border.color: theme.border
                                border.width: 1
                            }
                        }

                        Label {
                            text: "Model"
                            color: theme.dim
                            font.pixelSize: 10
                        }

                        ComboBox {
                            id: batchModelBox
                            objectName: "batchModelBox"
                            enabled: !audioBatch.running
                            Layout.columnSpan: 3
                            Layout.fillWidth: true
                            editable: true
                            model: filesPage.modelIdsForProvider(
                                filesPage.batchProviderId)
                            currentIndex: Math.max(
                                0, model.indexOf(filesPage.batchModelId))
                            onActivated: filesPage.selectBatchModel(currentText)
                            onAccepted: filesPage.commitBatchModel()
                            background: Rectangle {
                                implicitHeight: 26
                                radius: theme.controlRadius
                                color: theme.control
                                border.color: theme.border
                                border.width: 1
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 5

                        AppButton {
                            text: "Choose files"
                            theme: theme
                            enabled: !audioBatch.running
                            Layout.preferredWidth: 94
                            Layout.preferredHeight: 26
                            Accessible.name: "Choose local audio files"
                            onClicked: audioFileDialog.open()
                        }

                        Label {
                            Layout.fillWidth: true
                            text: audioBatch.selectedFiles.length === 0
                                  ? "No files selected"
                                  : audioBatch.selectedFiles.length + " file(s) selected"
                            color: theme.secondaryText
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 10
                        color: theme.resultSurface
                        border.color: theme.border
                        border.width: 1
                        clip: true

                        ScrollView {
                            anchors.fill: parent
                            anchors.margins: 6
                            clip: true

                            Column {
                                width: parent.width
                                spacing: 4

                                Repeater {
                                    model: audioBatch.results

                                    delegate: Rectangle {
                                        id: fileResultRow
                                        required property var modelData
                                        property bool hasTranscript: modelData.status === "succeeded"
                                                                      && modelData.text.length > 0
                                        property string copyLabel: "Copy"
                                        width: parent.width
                                        height: hasTranscript ? 166 : 34
                                        radius: 7
                                        color: theme.control

                                        Timer {
                                            id: transcriptCopyResetTimer
                                            interval: 900
                                            repeat: false
                                            onTriggered: fileResultRow.copyLabel = "Copy"
                                        }

                                        Connections {
                                            target: audioBatch

                                            function onCopyCompleted(path, success) {
                                                if (path !== fileResultRow.modelData.path)
                                                    return
                                                fileResultRow.copyLabel = success ? "Copied" : "Copy"
                                                if (success)
                                                    transcriptCopyResetTimer.restart()
                                            }
                                        }

                                        ColumnLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 8
                                            anchors.rightMargin: 8
                                            anchors.topMargin: 4
                                            anchors.bottomMargin: 4
                                            spacing: 3

                                            Label {
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 22
                                                text: fileResultRow.modelData.name
                                                color: theme.text
                                                font.pixelSize: 10
                                                elide: Text.ElideMiddle
                                            }

                                            Label {
                                                text: fileResultRow.modelData.status
                                                color: fileResultRow.modelData.status === "failed"
                                                       ? theme.secondaryText : theme.dim
                                                font.pixelSize: 9
                                            }

                                            ScrollView {
                                                visible: fileResultRow.hasTranscript
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 88
                                                clip: true

                                                TextArea {
                                                    id: transcriptText
                                                    width: parent.width
                                                    height: Math.max(implicitHeight, 88)
                                                    text: fileResultRow.modelData.text
                                                    readOnly: true
                                                    selectByMouse: true
                                                    wrapMode: TextEdit.Wrap
                                                    color: theme.secondaryText
                                                    selectionColor: theme.dim
                                                    selectedTextColor: theme.text
                                                    font.pixelSize: 10
                                                    padding: 0
                                                    background: Rectangle {
                                                        color: "transparent"
                                                    }
                                                    Accessible.name: "Transcript for "
                                                                      + fileResultRow.modelData.name
                                                }
                                            }

                                            RowLayout {
                                                visible: fileResultRow.hasTranscript
                                                Layout.fillWidth: true
                                                Layout.preferredHeight: 25
                                                spacing: 5

                                                Label {
                                                    text: "Select text to reuse"
                                                    color: theme.dim
                                                    font.pixelSize: 9
                                                    elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }

                                                AppButton {
                                                    text: fileResultRow.copyLabel
                                                    theme: theme
                                                    enabled: !audioBatch.running
                                                    Layout.preferredWidth: 58
                                                    Layout.preferredHeight: 24
                                                    Accessible.name: "Copy transcript for "
                                                                      + fileResultRow.modelData.name
                                                    onClicked: {
                                                        if (audioBatch.copyFile(fileResultRow.modelData.path))
                                                            fileResultRow.copyLabel = "Copying…"
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }

                                Label {
                                    visible: audioBatch.results.length === 0
                                    width: parent.width
                                    text: "Your selected files will appear here."
                                    color: theme.dim
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignHCenter
                                }
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: audioBatch.lastError !== ""
                        text: audioBatch.lastError
                        color: theme.secondaryText
                        font.pixelSize: 10
                        wrapMode: Text.WordWrap
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 5

                        AppButton {
                            text: "Start"
                            theme: theme
                            enabled: !audioBatch.running
                                     && audioBatch.selectedFiles.length > 0
                            Layout.preferredWidth: 54
                            Layout.preferredHeight: 26
                            Accessible.name: "Start audio transcription"
                            onClicked: filesPage.startBatch()
                        }

                        AppButton {
                            text: "Cancel"
                            theme: theme
                            quiet: true
                            enabled: audioBatch.running
                            Layout.preferredWidth: 58
                            Layout.preferredHeight: 26
                            Accessible.name: "Cancel audio transcription"
                            onClicked: audioBatch.cancel()
                        }

                        AppButton {
                            text: "Retry"
                            theme: theme
                            quiet: true
                            enabled: !audioBatch.running && audioBatch.canRetry
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Retry failed audio files"
                            onClicked: filesPage.retryBatch()
                        }

                        Item { Layout.fillWidth: true }

                        AppButton {
                            text: "Close"
                            theme: theme
                            quiet: true
                            enabled: !audioBatch.running
                            Layout.preferredWidth: 56
                            Layout.preferredHeight: 26
                            Accessible.name: "Close audio import"
                            onClicked: workflow.closeFiles()
                        }
                    }
                }
            }

            Item {
                id: translationPickerPage
                objectName: "translationPickerPage"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1

                            Label {
                                text: "Translate selection"
                                color: theme.text
                                font.pixelSize: 13
                                font.weight: Font.Bold
                            }

                            Label {
                                text: "Choose a target language"
                                color: theme.dim
                                font.pixelSize: 10
                            }
                        }

                        AppButton {
                            text: "—"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                            Accessible.name: "Cancel translation"
                            onClicked: workflow.cancelTranslation()
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: theme.border
                    }

                    Flow {
                        id: translationOptionsFlow
                        objectName: "translationOptionsFlow"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 86
                        spacing: 6

                        Repeater {
                            model: workflow.translationOptions

                            delegate: AppButton {
                                required property var modelData
                                text: modelData.label
                                theme: root.visualTheme
                                width: 154
                                height: 34
                                Accessible.name: "Translate to " + modelData.label
                                onClicked: workflow.chooseTranslation(modelData.code)
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    RowLayout {
                        Layout.fillWidth: true
                        Item { Layout.fillWidth: true }

                        AppButton {
                            text: "Cancel"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 56
                            Layout.preferredHeight: 26
                            Accessible.name: "Cancel translation"
                            onClicked: workflow.cancelTranslation()
                        }
                    }
                }
            }
        }
    }
}
