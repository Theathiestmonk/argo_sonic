import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

export default function Robot3DViewer({ shadowColor = '#e2b35c', modelPath = '/models/argo.glb', onLoadStart, onLoadEnd }) {
  const containerRef = useRef(null)
  const sceneRef = useRef(null)
  const cameraRef = useRef(null)
  const rendererRef = useRef(null)
  const modelRef = useRef(null)

  useEffect(() => {
    if (!containerRef.current) return

    // Clear previous renderer
    if (rendererRef.current && containerRef.current.contains(rendererRef.current.domElement)) {
      containerRef.current.removeChild(rendererRef.current.domElement)
      rendererRef.current.dispose()
    }

    // Scene setup
    const scene = new THREE.Scene()
    scene.background = null
    sceneRef.current = scene

    // Camera
    const camera = new THREE.PerspectiveCamera(
      45,
      containerRef.current.clientWidth / containerRef.current.clientHeight,
      0.1,
      1000
    )
    camera.position.set(0, 0.78, 4)
    cameraRef.current = camera

    // Renderer with shadow support
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setSize(containerRef.current.clientWidth, containerRef.current.clientHeight)
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setClearColor(0x000000, 0)
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFShadowShadowMap
    containerRef.current.appendChild(renderer.domElement)
    rendererRef.current = renderer

    // Lighting with shadows
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.6)
    scene.add(ambientLight)

    const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8)
    directionalLight.position.set(5, 5, 5)
    directionalLight.castShadow = true
    directionalLight.shadow.mapSize.width = 2048
    directionalLight.shadow.mapSize.height = 2048
    scene.add(directionalLight)

    // Add golden spotlight for shadow glow
    const spotLight = new THREE.SpotLight(0xe6b347, 0.6)
    spotLight.position.set(3, 8, 3)
    spotLight.castShadow = true
    scene.add(spotLight)

    // Load GLB model with shadows
    onLoadStart?.()
    const loader = new GLTFLoader()
    loader.load(
      modelPath,
      (gltf) => {
        onLoadEnd?.()
        const model = gltf.scene
        model.scale.set(2.8, 2.8, 2.8)
        model.position.set(0, -0.7, 0)

        // Enable shadows on all meshes
        model.traverse((child) => {
          if (child.isMesh) {
            child.castShadow = true
            child.receiveShadow = true
          }
        })

        scene.add(model)
        modelRef.current = model

      },
      (progress) => {
        console.log(`Model loading: ${(progress.loaded / progress.total * 100).toFixed(0)}%`)
      },
      (error) => {
        console.error('Error loading model:', error)
      }
    )

    // Mouse rotation controls
    let isMouseDown = false
    let mouseX = 0
    let mouseY = 0
    let targetRotationX = 0
    let targetRotationY = 0

    const onMouseDown = (e) => {
      isMouseDown = true
      mouseX = e.clientX
      mouseY = e.clientY
    }

    const onMouseMove = (e) => {
      if (!isMouseDown || !modelRef.current) return
      const deltaX = e.clientX - mouseX
      const deltaY = e.clientY - mouseY
      targetRotationY += deltaX * 0.005
      targetRotationX += deltaY * 0.005
      mouseX = e.clientX
      mouseY = e.clientY
    }

    const onMouseUp = () => {
      isMouseDown = false
    }

    containerRef.current.addEventListener('mousedown', onMouseDown)
    containerRef.current.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)

    // Animation loop
    let animationId
    const animate = () => {
      animationId = requestAnimationFrame(animate)

      // Smooth rotation
      if (modelRef.current) {
        modelRef.current.rotation.x += (targetRotationX - modelRef.current.rotation.x) * 0.1
        modelRef.current.rotation.y += (targetRotationY - modelRef.current.rotation.y) * 0.1
      }

      renderer.render(scene, camera)
    }
    animate()

    // Handle window resize
    const handleResize = () => {
      if (!containerRef.current) return
      const width = containerRef.current.clientWidth
      const height = containerRef.current.clientHeight
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
    }
    window.addEventListener('resize', handleResize)

    // Cleanup
    return () => {
      window.removeEventListener('resize', handleResize)
      if (containerRef.current) {
        containerRef.current.removeEventListener('mousedown', onMouseDown)
        containerRef.current.removeEventListener('mousemove', onMouseMove)
      }
      document.removeEventListener('mouseup', onMouseUp)
      cancelAnimationFrame(animationId)
    }
  }, [modelPath])

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        height: '400px',
      }}
    />
  )
}
