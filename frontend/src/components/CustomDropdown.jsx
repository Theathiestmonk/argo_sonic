import { useState, useRef, useEffect } from 'react'

export default function CustomDropdown({ options, value, onChange, label }) {
  const [isOpen, setIsOpen] = useState(false)
  const dropdownRef = useRef(null)

  // Close on outside click
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  const selectedOption = options.find(opt => opt.value === value)

  return (
    <div ref={dropdownRef} style={{ position: 'relative', width: '100%' }}>
      {label && (
        <div style={{ fontSize: 7, color: 'var(--muted)', textTransform: 'uppercase', fontWeight: 700, marginBottom: 6 }}>
          {label}
        </div>
      )}
      <button
        onClick={() => setIsOpen(!isOpen)}
        style={{
          width: '100%',
          padding: '10px 12px',
          borderRadius: 6,
          border: '1px solid rgba(226,179,92,0.3)',
          background: 'rgba(226,179,92,0.08)',
          color: 'var(--gold-bright)',
          fontSize: 13,
          fontWeight: 600,
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>{selectedOption?.label || 'Select...'}</span>
        <span style={{ fontSize: 10 }}>{isOpen ? '▲' : '▼'}</span>
      </button>

      {isOpen && (
        <div style={{
          position: 'absolute',
          top: '100%',
          left: 0,
          right: 0,
          marginTop: 4,
          background: 'rgba(20,20,30,0.95)',
          border: '1px solid rgba(226,179,92,0.3)',
          borderRadius: 6,
          zIndex: 1000,
          overflow: 'hidden',
          backdropFilter: 'blur(10px)',
          boxShadow: '0 8px 24px rgba(0,0,0,0.3)',
        }}>
          {options.map((option) => (
            <button
              key={option.value}
              onClick={() => {
                onChange(option.value)
                setIsOpen(false)
              }}
              style={{
                width: '100%',
                padding: '12px 14px',
                border: 'none',
                background: value === option.value ? 'rgba(226,179,92,0.2)' : 'transparent',
                color: value === option.value ? '#e2b35c' : 'rgba(226,179,92,0.7)',
                fontSize: 13,
                fontWeight: value === option.value ? 700 : 500,
                cursor: 'pointer',
                textAlign: 'left',
                borderLeft: value === option.value ? '3px solid #e2b35c' : '3px solid transparent',
                transition: 'all 0.2s ease',
              }}
              onMouseEnter={(e) => {
                e.target.style.background = 'rgba(226,179,92,0.15)'
                e.target.style.color = '#e2b35c'
              }}
              onMouseLeave={(e) => {
                e.target.style.background = value === option.value ? 'rgba(226,179,92,0.2)' : 'transparent'
                e.target.style.color = value === option.value ? '#e2b35c' : 'rgba(226,179,92,0.7)'
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
