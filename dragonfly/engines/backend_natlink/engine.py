﻿#
# This file is part of Dragonfly.
# (c) Copyright 2007, 2008 by Christo Butcher
# Licensed under the LGPL.
#
#   Dragonfly is free software: you can redistribute it and/or modify it
#   under the terms of the GNU Lesser General Public License as published
#   by the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   Dragonfly is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.
#
#   You should have received a copy of the GNU Lesser General Public
#   License along with Dragonfly.  If not, see
#   <http://www.gnu.org/licenses/>.
#

"""
SR back-end for DNS and Natlink
============================================================================

Detecting sleep mode
----------------------------------------------------------------------------

 - http://blogs.msdn.com/b/tsfaware/archive/2010/03/22/detecting-sleep-mode-in-sapi.aspx

"""

import os
import os.path
import pywintypes
import sys
import time
from datetime   import datetime
from locale     import getpreferredencoding
from threading  import Thread, Event

from six        import text_type, binary_type, string_types, PY2

from dragonfly.engines.base  import (EngineBase, EngineError, MimicFailure,
                                    GrammarWrapperBase)
from dragonfly.engines.backend_natlink.speaker    import NatlinkSpeaker
from dragonfly.engines.backend_natlink.compiler   import NatlinkCompiler
from dragonfly.engines.backend_natlink.dictation  import \
    NatlinkDictationContainer
from dragonfly.engines.backend_natlink.recobs     import \
    NatlinkRecObsManager
from dragonfly.engines.backend_natlink.timer      import NatlinkTimerManager


# ---------------------------------------------------------------------------

def map_word(word, encoding=getpreferredencoding(do_setlocale=False)):
    """
    Wraps output from Dragon.

    This wrapper ensures text output from the engine is Unicode. It assumes the
    encoding of byte streams is the current locale's preferred encoding by default.
    """
    if isinstance(word, text_type):
        return word
    elif isinstance(word, binary_type):
        return word.decode(encoding)
    return word


class TimerThread(Thread):
    """"""
    def __init__(self, engine):
        Thread.__init__(self)
        self._stop_event = Event()
        self.daemon = True
        self._timer = None
        self._engine = engine

    def start(self):
        if self._timer is None:
            def timer_function():
                # Let the thread run for a bit. This will yield control to
                # other threads.
                if self.is_alive():
                    self.join(0.0025)

            self._timer = self._engine.create_timer(timer_function, 0.025)

        Thread.start(self)

    def _stop_timer(self):
        if self._timer:
            self._timer.stop()
            self._timer = None

    def stop(self):
        self._stop_event.set()
        self._stop_timer()

    def run(self):
        while not self._stop_event.is_set():
            time.sleep(1)
        self._stop_timer()


class NatlinkEngine(EngineBase):
    """ Speech recognition engine back-end for Natlink and DNS. """

    _name = "natlink"
    DictationContainer = NatlinkDictationContainer

    #-----------------------------------------------------------------------

    def __init__(self, retain_dir=None):
        """
        :param retain_dir: directory to save audio data:
          A ``.wav`` file for each utterance, and ``retain.tsv`` file
          with each row listing (wav filename, wav length in seconds,
          grammar name, rule name, recognized text) as tab separated
          values.

          If this parameter is used in a module loaded by
          ``natlinkmain``, then the directory will be relative to the
          Natlink user directory (e.g. ``MacroSystem``).
        :type retain_dir: str|None
        """
        EngineBase.__init__(self)
        self._has_quoted_words_support = True

        self.natlink = None
        try:
            import natlink
        except ImportError:
            self._log.error("%s: failed to import natlink module." % self)
            raise EngineError("Requested engine 'natlink' is not "
                              "available: Natlink is not installed.")
        self.natlink = natlink

        self._grammar_count = 0
        self._recognition_observer_manager = NatlinkRecObsManager(self)
        self._timer_manager = NatlinkTimerManager(0.02, self)
        self._timer_thread = None
        self._retain_dir = None
        self._speaker = NatlinkSpeaker()
        try:
            self.set_retain_directory(retain_dir)
        except EngineError as err:
            self._retain_dir = None
            self._log.error(err)

    def apply_threading_fix(self):
        """
        Start a thread and engine timer internally to allow Python threads
        to work properly while connected to natlink. The fix is only applied
        once, successive calls have no effect.

        This method is called automatically when :meth:`connect` is called
        or when a grammar is loaded for the first time.
        """
        # Start a thread and engine timer to allow Python threads to work
        # properly while connected to Natlink.
        # Only start the thread if one isn't already active.
        if self._timer_thread is None:
            self._timer_thread = TimerThread(self)
            self._timer_thread.start()

    def connect(self):
        """ Connect to natlink with Python threading support enabled. """
        self.natlink.natConnect(True)
        self.apply_threading_fix()

    def disconnect(self):
        """ Disconnect from natlink. """
        # Unload all grammars from the engine so that Dragon doesn't keep
        # recognizing them.
        for grammar in self.grammars:
            grammar.unload()

        # Close the the waitForSpeech() dialog box if it is active for this
        # process.
        from dragonfly import Window
        target_title = "Natlink / Python Subsystem"
        for window in Window.get_matching_windows(title=target_title):
            if window.is_visible and window.pid == os.getpid():
                try:
                    window.close()
                except pywintypes.error:
                    pass
                break

        # Stop the special timer thread if it is running.
        if self._timer_thread:
            self._timer_thread.stop()
            self._timer_thread = None

        # Finally disconnect from natlink.
        self.natlink.natDisconnect()

    # -----------------------------------------------------------------------
    # Methods for working with grammars.

    def _load_grammar(self, grammar):
        """ Load the given *grammar* into natlink. """
        self._log.debug("Engine %s: loading grammar %s."
                        % (self, grammar.name))

        grammar_object = self.natlink.GramObj()
        wrapper = GrammarWrapper(grammar, grammar_object, self)
        grammar_object.setBeginCallback(wrapper.begin_callback)
        grammar_object.setResultsCallback(wrapper.results_callback)
        grammar_object.setHypothesisCallback(None)

        c = NatlinkCompiler()
        (compiled_grammar, rule_names) = c.compile_grammar(grammar)
        wrapper.rule_names = rule_names

        all_results = (hasattr(grammar, "process_recognition_other")
                       or hasattr(grammar, "process_recognition_failure"))
        hypothesis = False

        attempt_connect = False

        # Return early if the grammar has no rules.
        if not rule_names: return wrapper

        try:
            grammar_object.load(compiled_grammar, all_results, hypothesis)
        except self.natlink.NatError as e:
            # If loading failed because we're not connected yet,
            #  attempt to connect to natlink and reload the grammar.
            if (str(e) == "Calling GramObj.load is not allowed before"
                          " calling natConnect"):
                attempt_connect = True
            else:
                self._log.exception("Failed to load grammar %s: %s."
                                    % (grammar, e))
                raise EngineError("Failed to load grammar %s: %s."
                                  % (grammar, e))
        if attempt_connect:
            self.connect()
            try:
                grammar_object.load(compiled_grammar, all_results, hypothesis)
            except self.natlink.NatError as e:
                self._log.exception("Failed to load grammar %s: %s."
                                    % (grammar, e))
                raise EngineError("Failed to load grammar %s: %s."
                                  % (grammar, e))

        # Apply the threading fix if it hasn't been applied yet.
        self.apply_threading_fix()

        # Return the grammar wrapper.
        return wrapper

    def _unload_grammar(self, grammar, wrapper):
        """ Unload the given *grammar* from natlink. """
        try:
            grammar_object = wrapper.grammar_object
            grammar_object.setBeginCallback(None)
            grammar_object.setResultsCallback(None)
            grammar_object.setHypothesisCallback(None)
        except self.natlink.NatError as e:
            self._log.exception("Failed to unload grammar %s: %s."
                                % (grammar, e))

    def set_exclusiveness(self, grammar, exclusive):
        try:
            grammar_object = self._get_grammar_wrapper(grammar).grammar_object
            grammar_object.setExclusive(exclusive)
        except self.natlink.NatError as e:
            self._log.exception("Engine %s: failed set exclusiveness: %s."
                                % (self, e))

    def activate_grammar(self, grammar):
        self._log.debug("Activating grammar %s." % grammar.name)
        pass

    def deactivate_grammar(self, grammar):
        self._log.debug("Deactivating grammar %s." % grammar.name)
        pass

    def activate_rule(self, rule, grammar):
        self._log.debug("Activating rule %s in grammar %s." % (rule.name, grammar.name))
        wrapper = self._get_grammar_wrapper(grammar)
        if not wrapper: return
        wrapper.activate_rule(rule.name)

    def deactivate_rule(self, rule, grammar):
        self._log.debug("Deactivating rule %s in grammar %s." % (rule.name, grammar.name))
        wrapper = self._get_grammar_wrapper(grammar)
        if not wrapper: return
        wrapper.deactivate_rule(rule.name)

    def update_list(self, lst, grammar):
        wrapper = self._get_grammar_wrapper(grammar)
        if not wrapper:
            return
        grammar_object = wrapper.grammar_object

        # First empty then populate the list.  Use the local variables
        #  n and f as an optimization.
        n = lst.name
        f = grammar_object.appendList
        grammar_object.emptyList(n)
        [f(n, word) for word in lst.get_list_items()]

    #-----------------------------------------------------------------------
    # Miscellaneous methods.

    def _do_recognition(self):
        self.natlink.waitForSpeech()

    def mimic(self, words):
        """
        Mimic a recognition of the given *words*.

        .. note:: This method has a few quirks to be aware of:

            #. Mimic is not limited to one element per word as seen with
               proper nouns from DNS. For example, "Buffalo Bills" can be
               passed as one word.
            #. Mimic can handle by the extra formatting by DNS built-in
               commands.
            #. Mimic is case sensitive.

        """
        if isinstance(words, string_types):
            words = words.split()

        try:
            prepared_words = []
            if PY2:
                encoding = getpreferredencoding()
                for word in words:
                    if isinstance(word, text_type):
                        word = word.encode(encoding)
                    prepared_words.append(word)
            else:
                for word in words:
                    prepared_words.append(word)
            if len(prepared_words) == 0:
                raise TypeError("empty list or string")
        except Exception as e:
            raise MimicFailure("Invalid mimic input %r: %s."
                               % (words, e))
        try:
            self.natlink.recognitionMimic(prepared_words)
        except self.natlink.MimicFailed:
            raise MimicFailure("No matching rule found for words %r."
                               % (prepared_words,))

    def speak(self, text):
        """ Speak the given *text* using text-to-speech. """
        self._speaker.speak(text)

    def _get_language(self):
        # Get a Windows language identifier from Dragon.
        import win32com.client
        app = win32com.client.Dispatch("Dragon.DgnEngineControl")
        language = app.SpeakerLanguage("")

        # Lookup and return the language tag.
        return self._get_language_tag(language)

    def set_retain_directory(self, retain_dir):
        """
        Set the directory where audio data is saved.

        Retaining audio data may be useful for acoustic model training. This
        is disabled by default.

        If a relative path is used and the code is running via natspeak.exe,
        then the path will be made relative to the Natlink user directory or
        base directory (e.g. ``MacroSystem``).

        :param retain_dir: retain directory path
        :type retain_dir: string|None
        """
        is_string = isinstance(retain_dir, string_types)
        if not (retain_dir is None or is_string):
            raise EngineError("Invalid retain_dir: %r" % retain_dir)

        if is_string:
            # Handle relative paths by using the Natlink user directory or
            # base directory. Only do this if running via natspeak.exe.
            try:
                import natlinkstatus
            except ImportError:
                try:
                    from natlinkcore import natlinkstatus
                except ImportError:
                    natlinkstatus = None
            running_via_natspeak = (
                sys.executable.endswith("natspeak.exe") and
                natlinkstatus is not None
            )
            if not os.path.isabs(retain_dir) and running_via_natspeak:
                status = natlinkstatus.NatlinkStatus()
                user_dir = status.getUserDirectory()
                retain_dir = os.path.join(
                    # Use the base dir if user dir isn't set.
                    user_dir if user_dir else status.BaseDirectory,
                    retain_dir
                )

        # Set the retain directory.
        self._retain_dir = retain_dir

#---------------------------------------------------------------------------


class GrammarWrapper(GrammarWrapperBase):

    # Enable guessing at which words were dictated, since DNS does not
    # always report accurate rule IDs.
    _dictated_word_guesses_enabled = True

    def __init__(self, grammar, grammar_object, engine):
        GrammarWrapperBase.__init__(self, grammar, engine)
        self.grammar_object = grammar_object
        self.rule_names = None
        self.active_rules_set = set()
        self.hwnd = 0
        self.beginning = False

    def begin_callback(self, module_info):
        self.beginning = True
        executable, title, handle = tuple(map_word(word)
                                          for word in module_info)
        self.hwnd = handle

        # Run the grammar's process_begin() method.
        try:
            self.grammar.process_begin(executable, title, handle)
        except:
            message = "Exception occurred during process_begin() call."
            self._log.exception(message)

        # Ensure that active rules are active for the current window.
        self.beginning = False
        for rule_name in self.active_rules_set:
            self.activate_rule(rule_name)

    def activate_rule(self, rule_name):
        self.active_rules_set.add(rule_name)

        # Rule activation is delayed.
        if self.beginning: return

        # Activate the rule for the current window.
        grammar_object = self.grammar_object
        try:
            grammar_object.activate(rule_name, self.hwnd)
        except self.natlink.NatError:
            grammar_object.deactivate(rule_name)
            grammar_object.activate(rule_name, self.hwnd)

    def deactivate_rule(self, rule_name):
        self.active_rules_set.remove(rule_name)
        grammar_object = self.grammar_object
        grammar_object.deactivate(rule_name)

    def results_callback(self, words, results):
        self._log.debug("Grammar %s: received recognition %r.",
                        self.grammar.name, words)

        if words == "other":
            result_words = tuple(map_word(w) for w in results.getWords(0))
            self.recognition_other_callback(result_words, results)
            return
        elif words == "reject":
            self.recognition_failure_callback(results)
            return

        # If the words argument was not "other" or "reject", then
        #  it is a sequence of (word, rule_id) 2-tuples.  Convert this
        #  into a tuple of unicode objects.
        words_rules = tuple((map_word(w), r) for w, r in words)

        # Process this recognition without dispatching results to other
        #  grammars; Natlink handles this for us perfectly.
        if self.process_results(words_rules, self.rule_names, results,
                                dispatch_other=False):
            return

        # Failed to decode recognition.
        words = tuple(w for w, r in words_rules)
        self._log.error("Grammar %s: failed to decode recognition %r.",
                        self.grammar._name, words)

    def _process_final_rule(self, state, words, results, dispatch_other,
                            rule, *args):
        # Retain audio, if appropriate.
        self._retain_audio(words, results, rule.name)

        # Call the base class method.
        GrammarWrapperBase._process_final_rule(self, state, words, results,
                                               dispatch_other, rule, *args)

    # TODO Extract the retain audio feature into an example command module.
    #  A grammar with a `process_recognition_other' function should be able
    #  to handle this without issue.
    def _retain_audio(self, words, results, rule_name):
        # Only write audio data and metadata if the directory exists.
        retain_dir = self.engine._retain_dir
        if retain_dir and not os.path.isdir(retain_dir):
            self.engine._log.warning(
                "Audio was not retained because '%s' was not a "
                "directory" % retain_dir
            )
        elif retain_dir:
            try:
                audio = results.getWave()
                # Make sure we have audio data
                if len(audio) > 0:
                    # Write audio data.
                    now = datetime.now()
                    filename = ("retain_%s.wav"
                                % now.strftime("%Y-%m-%d_%H-%M-%S_%f"))
                    wav_path = os.path.join(retain_dir, filename)
                    with open(wav_path, "wb") as f:
                        f.write(audio)

                    # Write metadata, assuming 11025Hz 16bit mono audio
                    text = ' '.join(words)
                    audio_length = float(len(audio) / 2) / 11025
                    tsv_path = os.path.join(retain_dir, "retain.tsv")
                    with open(tsv_path, "a") as tsv_file:
                        tsv_file.write('\t'.join([
                            filename, text_type(audio_length),
                            self.grammar.name, rule_name, text
                        ]) + '\n')
            except:
                self._log.exception("Exception retaining audio")
