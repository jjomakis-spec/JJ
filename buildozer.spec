import kivy
kivy.require('2.0.0')

from kivy.lang import Builder
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.app import App
from kivy.properties import ObjectProperty, StringProperty
from kivy.uix.button import Button
from kivy.uix.bubble import Bubble

KV = '''
Controller:
    label_wid: my_custom_label
    info: 'Hello world'

    BoxLayout:
        orientation: 'horizontal'
        padding: 20

        Button:
            text: 'My controller info is: ' + root.info
            on_press: root.do_action()

        Label:
            id: my_custom_label
            text: 'My label before button press'
'''

class Controller(FloatLayout):
    # label_wid = ObjectProperty()
    info = StringProperty()
    isShow = True
    def do_action(self, label):
        if(self.isShow):
            self.ids.box.remove_widget(label)
            self.isShow = False
        else:
            self.ids.box.add_widget(label)
            self.isShow = True
        # label.text = 'test label'
        # self.label_wid.text = 'My label after button press'
        self.info = 'New info text'



class ControllerApp(App):
    def build(self):
        layout = Bubble()
        layout.add_widget(Button(text='Hello 1'))
        layout.add_widget(Button(text='World 1'))
        # layout.add_widget(Button(text='aaa', size_hint_x=None, width=300))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        # layout.add_widget(Button(text='World 2'))
        return layout
        # return Controller(info='Hello world')
        # return Builder.load_string(KV)


if __name__ == '__main__':
    ControllerApp().run()
