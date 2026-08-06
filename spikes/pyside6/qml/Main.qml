import QtQuick 6.5
import QtQuick.Controls 6.5
import QtQuick.Layouts 6.5
import QtQuick.Window 6.5

ApplicationWindow {
    id: root
    objectName: "clarifyVoiceQmlPilot"
    width: workflow.surface === "result" ? theme.resultWidth
           : (workflow.surface === "settings"
              || workflow.surface === "translation_picker")
             ? theme.panelWidth : theme.windowWidth
    height: workflow.surface === "result"
            ? theme.resultHeight
            : (workflow.surface === "settings"
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
                          ? 1
                          : workflow.surface === "settings" ? 2
                          : workflow.surface === "translation_picker" ? 3 : 0

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

                        PilotButton {
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
                                workflow.setMode(homePage.promptMode
                                                 ? "transcription" : "prompt")
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

                    RowLayout {
                        id: errorControls
                        visible: workflow.surface === "error"
                        spacing: 4

                        PilotButton {
                            text: "Dismiss"
                            theme: theme
                            Layout.preferredWidth: 60
                            Layout.preferredHeight: 26
                            Accessible.name: "Dismiss workflow error"
                            onClicked: workflow.reset()
                        }

                        Item { Layout.fillWidth: true }
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
                            onClicked: workflow.copyResult()
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

                            Rectangle {
                                Layout.fillWidth: true
                                height: 1
                                color: theme.border
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

                        PilotButton {
                            text: "Load"
                            theme: theme
                            quiet: true
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Reload settings"
                            onClicked: settings.load()
                        }

                        PilotButton {
                            text: "Save"
                            theme: theme
                            enabled: settings.dirty
                            Layout.preferredWidth: 52
                            Layout.preferredHeight: 26
                            Accessible.name: "Save settings"
                            onClicked: settings.save()
                        }

                        PilotButton {
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

                        PilotButton {
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

                            delegate: PilotButton {
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

                        PilotButton {
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
